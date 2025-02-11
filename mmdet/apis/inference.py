import warnings

import matplotlib.pyplot as plt
import mmcv
import torch
from mmcv.ops import RoIAlign, RoIPool
from mmcv.parallel import collate, scatter
from mmcv.runner import load_checkpoint

from mmdet.core import get_classes
from mmdet.datasets.pipelines import Compose
from mmdet.models import build_detector


def init_detector(config, checkpoint=None, device='cuda:0'):
    """Initialize a detector from config file.

    Args:
        config (str or :obj:`mmcv.Config`): Config file path or the config
            object.
        checkpoint (str, optional): Checkpoint path. If left as None, the model
            will not load any weights.

    Returns:
        nn.Module: The constructed detector.
    """
    if isinstance(config, str):
        config = mmcv.Config.fromfile(config)
    elif not isinstance(config, mmcv.Config):
        raise TypeError('config must be a filename or Config object, '
                        f'but got {type(config)}')
    config.model.pretrained = None
    model = build_detector(config.model, test_cfg=config.test_cfg)
    if checkpoint is not None:
        map_loc = 'cpu' if device == 'cpu' else None
        checkpoint = load_checkpoint(model, checkpoint, map_location=map_loc)
        if 'CLASSES' in checkpoint['meta']:
            model.CLASSES = checkpoint['meta']['CLASSES']
        else:
            warnings.simplefilter('once')
            warnings.warn('Class names are not saved in the checkpoint\'s '
                          'meta data, use COCO classes by default.')
            model.CLASSES = get_classes('coco')
    model.cfg = config  # save the config in the model for convenience
    model.to(device)
    model.eval()
    return model


class LoadImage(object):
    """A simple pipeline to load image."""

    def __call__(self, results):
        """Call function to load images into results.

        Args:
            results (dict): A result dict contains the file name
                of the image to be read.

        Returns:
            dict: ``results`` will be returned containing loaded image.
        """
        if isinstance(results['img'], str):
            results['filename'] = results['img']
            results['ori_filename'] = results['img']
        else:
            results['filename'] = None
            results['ori_filename'] = None
        img = mmcv.imread(results['img'])
        results['img'] = img
        results['img_fields'] = ['img']
        results['img_shape'] = img.shape
        results['ori_shape'] = img.shape
        return results


def inference_detector(model, img):
    """Inference image(s) with the detector.

    Args:
        model (nn.Module): The loaded detector.
        imgs (str/ndarray or list[str/ndarray]): Either image files or loaded
            images.

    Returns:
        If imgs is a str, a generator will be returned, otherwise return the
        detection results directly.
    """
    cfg = model.cfg
    device = next(model.parameters()).device  # model device
    # build the data pipeline
    test_pipeline = [LoadImage()] + cfg.data.test.pipeline[1:]
    test_pipeline = Compose(test_pipeline)
    # prepare data
    data = dict(img=img)
    data = test_pipeline(data)
    data = collate([data], samples_per_gpu=1)
    if next(model.parameters()).is_cuda:
        # scatter to specified GPU
        data = scatter(data, [device])[0]
    else:
        # Use torchvision ops for CPU mode instead
        for m in model.modules():
            if isinstance(m, (RoIPool, RoIAlign)):
                if not m.aligned:
                    # aligned=False is not implemented on CPU
                    # set use_torchvision on-the-fly
                    m.use_torchvision = True
        warnings.warn('We set use_torchvision=True in CPU mode.')
        # just get the actual data from DataContainer
        data['img_metas'] = data['img_metas'][0].data

    # forward the model
    with torch.no_grad():
        data['img'][0] = data['img'][0].cuda()
        data.update({'x': data.pop('img')})
        result = model(return_loss=False, rescale=True, **data)
        y_head_f_1, y_head_f_2, y_head_cls = model(return_loss=False, rescale=True, return_box=False, **data)
        y_head_f_1 = torch.cat(y_head_f_1, 0)
        y_head_f_2 = torch.cat(y_head_f_2, 0)
        y_head_f_1 = torch.nn.Sigmoid()(y_head_f_1)
        y_head_f_2 = torch.nn.Sigmoid()(y_head_f_2)
        if cfg.uncertainty_type == 'l2_norm':
            loss_p = (y_head_f_1 - y_head_f_2).pow(2)
            uncertainty_all_N = loss_p.mean(dim=1)
        elif cfg.uncertainty_type == 'kl_divergence':
            loss_p = y_head_f_1 * torch.log(y_head_f_1 / y_head_f_2)
            # uncertainty_all_N = loss_p.sum(dim=1)
            uncertainty_all_N = loss_p.mean(dim=1)
        elif cfg.uncertainty_type == 'cross_entropy':
            loss_p = -(y_head_f_1 * torch.log(y_head_f_2) + (1 - y_head_f_1) * torch.log(1 - y_head_f_2))
            # uncertainty_all_N = loss_p.sum(dim=1)
            uncertainty_all_N = loss_p.mean(dim=1)
        elif cfg.uncertainty_type == 'js_divergence':
            kl_diver = lambda p, q: p * torch.log(p / q)
            loss_p = 0.5 * (kl_diver(y_head_f_1, (y_head_f_1 + y_head_f_2) / 2))
            loss_p += 0.5 * (kl_diver(y_head_f_2, (y_head_f_1 + y_head_f_2) / 2))
            # uncertainty_all_N = loss_p.sum(dim=1)
            uncertainty_all_N = loss_p.mean(dim=1)
        elif cfg.uncertainty_type == 'focal_loss':
            alpha, gamma = 0.25, 2.
            p_t = (y_head_f_1 * y_head_f_2) + ((1 - y_head_f_1) * (1 - y_head_f_2))
            alpha_factor = 1.
            modulating_factor = 1.
            ce = -(y_head_f_1 * torch.log(y_head_f_2) + (1 - y_head_f_1) * torch.log(1 - y_head_f_2))
            # ce = loss_p.sum(dim=1)
            if alpha:
                alpha_factor = y_head_f_1 * alpha + (1 - y_head_f_1) * (1 - alpha)
            if gamma:
                modulating_factor = (1. - p_t).pow(gamma)
            # uncertainty_all_N = (alpha_factor * modulating_factor * ce).sum(dim=1)
            uncertainty_all_N = (alpha_factor * modulating_factor * ce).mean(dim=1)
        else:
            uncertainty_types = [
                'l2_norm', 'kl_divergence', 'cross_entropy', 'js_divergence', 'focal_loss']
            raise ValueError(
                f'{cfg.uncertainty_type} is not valid uncertainty_type. '
                f'List of possible uncertainty_type: {uncertainty_types}. Change it in config parameters.')
        arg = uncertainty_all_N.argsort()
        uncertainty_single = uncertainty_all_N[arg[-cfg.k:]].mean()
    return result, uncertainty_single


async def async_inference_detector(model, img):
    """Async inference image(s) with the detector.

    Args:
        model (nn.Module): The loaded detector.
        imgs (str/ndarray or list[str/ndarray]): Either image files or loaded
            images.

    Returns:
        Awaitable detection results.
    """
    cfg = model.cfg
    device = next(model.parameters()).device  # model device
    # build the data pipeline
    test_pipeline = [LoadImage()] + cfg.data.test.pipeline[1:]
    test_pipeline = Compose(test_pipeline)
    # prepare data
    data = dict(img=img)
    data = test_pipeline(data)
    data = scatter(collate([data], samples_per_gpu=1), [device])[0]

    # We don't restore `torch.is_grad_enabled()` value during concurrent
    # inference since execution can overlap
    torch.set_grad_enabled(False)
    result = await model.aforward_test(rescale=True, **data)
    return result


def show_result_pyplot(model, img, result, score_thr=0.3, fig_size=(15, 10)):
    """Visualize the detection results on the image.

    Args:
        model (nn.Module): The loaded detector.
        img (str or np.ndarray): Image filename or loaded image.
        result (tuple[list] or list): The detection result, can be either
            (bbox, segm) or just bbox.
        score_thr (float): The threshold to visualize the bboxes and masks.
        fig_size (tuple): Figure size of the pyplot figure.
    """
    if hasattr(model, 'module'):
        model = model.module
    img = model.show_result(img, result, score_thr=score_thr, show=False)
    plt.figure(figsize=fig_size)
    plt.imshow(mmcv.bgr2rgb(img))
    plt.show()



# if __name__ == '__main__':
#     model = init_detector(
#         './configs/MIAOD.py', checkpoint='./work_dirs/MI-AOD/20220422_201426/epoch_3.pth', device='cuda:0')
#     results = {'img': './data/VOCdevkit/VOC2012/JPEGImages/2007_000027.jpg'}
#     img_loader = LoadImage()
#     img = img_loader(results)
#     res = inference_detector(model, img['img'])
#     k=0