import torch
from torch import Tensor
import numpy as np
from numpy import pi, exp, sqrt

from modules import devices, shared
from modules.shared import state
from modules.script_callbacks import before_image_saved_callback, remove_callbacks_for_function

from methods.abstractdiffusion import TiledDiffusion


class MixtureOfDiffusers(TiledDiffusion):
    """
        MixtureOfDiffusers Implementation
        Hijack the UNet for latent noise tiling and fusion
    """

    def __init__(self, *args, **kwargs):
        super().__init__("Mixture of Diffusers", *args, **kwargs)

        self.custom_weights = []

    def _gaussian_weights(self, tile_w=None, tile_h=None) -> Tensor:
        '''
        Copy from the original implementation of Mixture of Diffusers
        https://github.com/albarji/mixture-of-diffusers/blob/master/mixdiff/tiling.py
        This generates gaussian weights to smooth the noise of each tile.
        This is critical for this method to work.
        '''
        if tile_w is None: tile_w = self.tile_w
        if tile_h is None: tile_h = self.tile_h
        
        f = lambda x, midpoint, var=0.01: exp(-(x-midpoint)*(x-midpoint) / (tile_w*tile_w) / (2*var)) / sqrt(2*pi*var)
        x_probs = [f(x, (tile_w - 1) / 2) for x in range(tile_w)]   # -1 because index goes from 0 to latent_width - 1
        y_probs = [f(y,  tile_h      / 2) for y in range(tile_h)]

        w = np.outer(y_probs, x_probs)
        return torch.from_numpy(w).to(devices.device, dtype=torch.float32)

    def get_global_weights(self):
        if not hasattr(self, 'per_tile_weights'):
            self.per_tile_weights = self._gaussian_weights()
        return self.per_tile_weights

    def init(self, x_in):
        super().init(x_in)

        if not hasattr(self, 'rescale_factor'):
            # The original gaussian weights can be extremely small, so we rescale them for numerical stability
            self.rescale_factor = 1 / self.weights
            # Meanwhile, we rescale the custom weights in advance to save time of slicing
            for bbox_id, (bbox, _, _, _) in enumerate(self.custom_bboxes):
                self.custom_weights[bbox_id] = self.custom_weights[bbox_id].to(device=x_in.device, dtype=x_in.dtype)
                self.custom_weights[bbox_id] *= self.rescale_factor[:, :, bbox[1]:bbox[3], bbox[0]:bbox[2]]
            
    def init_custom_bbox(self, global_multiplier, bbox_control_states, *args, **kwargs):
        super().init_custom_bbox(global_multiplier, bbox_control_states, *args, **kwargs)
        for bbox, _, _, m in self.custom_bboxes:
            gaussian_weights = self._gaussian_weights(bbox[2] - bbox[0], bbox[3] - bbox[1]) * m
            self.weights[:, :, bbox[1]:bbox[3], bbox[0]:bbox[2]] += gaussian_weights
            self.custom_weights.append(gaussian_weights.unsqueeze(0).unsqueeze(0))

    def hook(self):
        if not hasattr(shared.sd_model, 'md_org_apply_model'):
            shared.sd_model.md_org_apply_model = shared.sd_model.apply_model
            shared.sd_model.apply_model = self.apply_model

        def remove_hook(_):
            MixtureOfDiffusers.unhook()
            remove_callbacks_for_function(MixtureOfDiffusers.unhook)
        before_image_saved_callback(remove_hook)

    @staticmethod
    def unhook():
        if hasattr(shared.sd_model, 'md_org_apply_model'):
            shared.sd_model.apply_model = shared.sd_model.md_org_apply_model
            del shared.sd_model.md_org_apply_model

    def custom_apply_model(self, x_in, t_in, c_in, bbox_id, bbox, cond, uncond):
        if self.is_kdiff:
            return self.kdiff_custom_forward(x_in, c_in, cond, uncond, bbox_id, bbox, 
                                             sigma_in=t_in, forward_func=shared.sd_model.md_org_apply_model)
        else:
            def forward_func(x, c, ts, unconditional_conditioning, *args, **kwargs):
                # copy from p_sample_ddim in ddim.py
                c_in = dict()
                for k in c:
                    if isinstance(c[k], list):
                        c_in[k] = [torch.cat([unconditional_conditioning[k][i], c[k][i]]) for i in range(len(c[k]))]
                    else:
                        c_in[k] = torch.cat([unconditional_conditioning[k], c[k]])
                self.set_control_tensor(bbox_id, x.shape[0])
                return shared.sd_model.md_org_apply_model(x, ts, c_in)
            return self.ddim_custom_forward(x_in, c_in, cond, uncond, bbox, ts=t_in, forward_func=forward_func)

    @torch.no_grad()
    def apply_model(self, x_in, t_in, cond):
        '''
        Hook to UNet when predicting noise
        '''
        # KDiffusion Compatibility
        c_in = cond
        N, C, H, W = x_in.shape
        assert H == self.h and W == self.w

        self.init(x_in)

        # Global sampling
        if self.global_multiplier > 0:
            for batch_id, bboxes in enumerate(self.batched_bboxes):
                if state.interrupted: return x_in
                
                x_tile_list = []
                t_tile_list = []
                attn_tile_list = []
                image_cond_list = []
                for bbox in bboxes:
                    x_tile_list.append(x_in[:, :, bbox[1]:bbox[3], bbox[0]:bbox[2]])
                    t_tile_list.append(t_in)
                    if c_in is not None and isinstance(cond, dict):
                        image_cond = cond['c_concat'][0]
                        if image_cond.shape[2] == self.h and image_cond.shape[3] == self.w:
                            image_cond = image_cond[:, :, bbox[1]:bbox[3], bbox[0]:bbox[2]]
                        image_cond_list.append(image_cond)
                        attn_tile = cond['c_crossattn'][0]
                        attn_tile_list.append(attn_tile)
                x_tile = torch.cat(x_tile_list, dim=0)
                t_tile = torch.cat(t_tile_list, dim=0)
                attn_tile = torch.cat(attn_tile_list, dim=0)
                image_cond_tile = torch.cat(image_cond_list, dim=0)
                c_tile = {'c_concat': [image_cond_tile], 'c_crossattn': [attn_tile]}
                
                # Controlnet tiling
                self.switch_controlnet_tensors(batch_id, N, len(bboxes), is_denoise=True)
                x_tile_out = shared.sd_model.md_org_apply_model(x_tile, t_tile, c_tile)  # here the x is the noise

                for i, bbox in enumerate(bboxes):
                    # This weights can be calcluated in advance, but will cost a lot of vram 
                    # when you have many tiles. So we calculate it here.
                    w = self.per_tile_weights * self.rescale_factor[:, :, bbox[1]:bbox[3], bbox[0]:bbox[2]]
                    self.x_buffer[:, :, bbox[1]:bbox[3], bbox[0]:bbox[2]] += x_tile_out[i*N:(i+1)*N, :, :, :] * w

                self.update_pbar()

        # Custom region sampling
        if len(self.custom_bboxes) > 0:
            if self.global_multiplier > 0 and abs(self.global_multiplier - 1.0) > 1e-6:
                self.x_buffer *= self.global_multiplier
            
            for bbox_id, (bbox, cond, uncond, _) in enumerate(self.custom_bboxes):
                # unpack sigma_in, x_in, image_cond
                x_tile = x_in[:, :, bbox[1]:bbox[3], bbox[0]:bbox[2]]
                x_tile_out = self.custom_apply_model(x_tile, t_in, c_in, bbox_id, bbox, cond, uncond)
                x_tile_out *= self.custom_weights[bbox_id]
                self.x_buffer[:, :, bbox[1]:bbox[3], bbox[0]:bbox[2]] += x_tile_out
                self.update_pbar()

        return self.x_buffer
