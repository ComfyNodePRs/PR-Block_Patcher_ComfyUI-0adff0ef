from comfy_extras.nodes_custom_sampler import Noise_RandomNoise, BasicScheduler, BasicGuider, SamplerCustomAdvanced
from comfy_extras.nodes_latent import LatentBatch
from comfy_extras.nodes_model_advanced import ModelSamplingFlux, ModelSamplingAuraFlow
from node_helpers import conditioning_set_values
import comfy.samplers
import re
import os
from pathlib import Path
import torch
import torch.nn.functional as F
import torchvision.transforms.v2 as T
#import folder_paths

FONTS_DIR = os.path.join(os.path.dirname(os.path.realpath(__file__)), "fonts")

class FluxBlockPatcherSampler:
    @classmethod
    def INPUT_TYPES(s):
        return {
            "required": {
                "model": ("MODEL", ),
                "conditioning": ("CONDITIONING", ),
                "latent_image": ("LATENT", ),
                
                "noise_seed": ("INT", {"default": 0, "min": 0, "max": 0xffffffffffffffff}),
                "steps": ("INT", {"default": 24, "min": 1, "max": 10000}),
                "sampler": (comfy.samplers.KSampler.SAMPLERS, ),
                "scheduler": (comfy.samplers.KSampler.SCHEDULERS, ),
                "guidance": ("FLOAT", {"default": 3.5, "min": -10.0, "max": 10.0, "step": 0.1}),

                "blocks": ("STRING", { "multiline": True, "dynamicPrompts": True, "default": "double_blocks\.([0-9]+)\.(img|txt)_(mod|attn|mlp\.[02])\.(lin|qkv|proj)\.(weight|bias)=1.1\nsingle_blocks\.([0-9]+)\.(linear[12]|modulation\.lin)\.(weight|bias)=1.1" }),
            }
        }

    RETURN_TYPES = ("LATENT", "SAMPLER_PARAMS", "STRING",)
    RETURN_NAMES = ("latent", "sampler_params", "patched_blocks",)
    FUNCTION = "apply_style"

    def apply_style(self, model, conditioning, latent_image, noise_seed, steps, sampler, scheduler, guidance, blocks):
        #is_schnell = model.model.model_type == comfy.model_base.ModelType.FLOW

        sd = model.model_state_dict()

        blocks = blocks.split("\n")
        blocks = [b.strip() for b in blocks if b.strip()]

        patched_blocks = []
        fbi_params = []
        out_latent = None

        noise = Noise_RandomNoise(noise_seed)
        sigmas = BasicScheduler().get_sigmas(model, scheduler, steps, 1.0)[0]
        cond = conditioning_set_values(conditioning, {"guidance": guidance})
        sca = SamplerCustomAdvanced()
        latentbatch = LatentBatch()
        samplerobject = comfy.samplers.sampler_object(sampler)

        for b in blocks:
            b = b.split("=")
            block = b[0].strip()
            value = float(b[1].strip())
            m = model.clone()
            out = { "regex": block, "value": value, "blocks": [] }

            for k in sd:
                if re.search(block, k):
                    m.add_patches({k: (None,)}, 0.0, value)
                    patched_blocks.append(f"{k}: {value}")
                    out["blocks"].append(k)
            
            guider = BasicGuider().get_guider(m, cond)[0]
            latent = sca.sample(noise, guider, samplerobject, sigmas, latent_image)[1]
            fbi_params.append(out)

            if out_latent is None:
                out_latent = latent
            else:
                out_latent = latentbatch.batch(out_latent, latent)[0]

            #m = None
            #del m

        patched_blocks = "\n".join(patched_blocks)

        return (out_latent, fbi_params, patched_blocks)

class PlotBlockParams:
    @classmethod
    def INPUT_TYPES(s):
        return {"required": {
                    "images": ("IMAGE", ),
                    "params": ("SAMPLER_PARAMS", ),
                    "cols_num": ("INT", {"default": -1, "min": -1, "max": 1024 }),
                    "add_params": (["false", "true"], {"default": "true"}),
                }}

    RETURN_TYPES = ("IMAGE", )
    FUNCTION = "execute"
    CATEGORY = "essentials/sampling"

    def execute(self, images, params, cols_num, add_params):
        from PIL import Image, ImageDraw, ImageFont
        import math
        #import textwrap

        if images.shape[0] != len(params):
            raise ValueError("Number of images and number of parameters do not match.")

        _params = params.copy()

        if cols_num == 0:
            cols_num = int(math.sqrt(images.shape[0]))
            cols_num = max(1, min(cols_num, 1024))

        width = images.shape[2]
        out_image = []

        font = ImageFont.truetype(os.path.join(FONTS_DIR, 'ShareTechMono-Regular.ttf'), min(32, int(20*(width/1024))))
        text_padding = 3
        line_height = font.getmask('Q').getbbox()[3] + font.getmetrics()[1] + text_padding*2
        #char_width = font.getbbox('M')[2]+1 # using monospace font

        for (image, param) in zip(images, _params):
            image = image.permute(2, 0, 1)

            if add_params != "false":
                text = f"{param['regex']}: {param['value']}"

                lines = text.split("\n")
                text_height = line_height * len(lines)
                text_image = Image.new('RGB', (width, text_height), color=(0, 0, 0))

                for i, line in enumerate(lines):
                    draw = ImageDraw.Draw(text_image)
                    draw.text((text_padding, i * line_height + text_padding), line, font=font, fill=(255, 255, 255))

                text_image = T.ToTensor()(text_image).to(image.device)
                image = torch.cat([image, text_image], 1)

            # a little cleanup
            image = torch.nan_to_num(image, nan=0.0).clamp(0.0, 1.0)
            out_image.append(image)

        out_image = torch.stack(out_image, 0).permute(0, 2, 3, 1)

        # merge images
        if cols_num > -1:
            cols = min(cols_num, out_image.shape[0])
            b, h, w, c = out_image.shape
            rows = math.ceil(b / cols)

            # Pad the tensor if necessary
            if b % cols != 0:
                padding = cols - (b % cols)
                out_image = F.pad(out_image, (0, 0, 0, 0, 0, 0, 0, padding))
                b = out_image.shape[0]

            # Reshape and transpose
            out_image = out_image.reshape(rows, cols, h, w, c)
            out_image = out_image.permute(0, 2, 1, 3, 4)
            out_image = out_image.reshape(rows * h, cols * w, c).unsqueeze(0)

        return (out_image, )


NODE_CLASS_MAPPINGS = {
    "FluxBlockPatcherSampler": FluxBlockPatcherSampler,
    "PlotBlockParams": PlotBlockParams,
}

NODE_DISPLAY_NAME_MAPPINGS = {
    "FluxBlockPatcherSampler": "Flux Block Patcher Sampler",
    "PlotBlockParams": "Plot Block Params",
}
