from abc import ABCMeta, abstractmethod
from functools import wraps
from typing import Callable, Dict, List, Optional, Tuple, Union
import torch
import torch.distributed
import torch.nn as nn

from diffusers import DiffusionPipeline
from diffusers.models.autoencoders.autoencoder_kl import AutoencoderKL

from distvae.modules.adapters.vae.decoder_adapters import DecoderAdapter
from xfuser.config.config import (
    EngineConfig,
    InputConfig,
)
from xfuser.core.distributed.parallel_state import get_tensor_model_parallel_world_size
from xfuser.logger import init_logger
from xfuser.core.distributed import (
    get_data_parallel_world_size,
    get_sequence_parallel_world_size,
    get_pipeline_parallel_world_size,
    get_classifier_free_guidance_world_size,
    get_classifier_free_guidance_rank,
    is_pipeline_first_stage,
    is_pipeline_last_stage,
    get_pp_group,
    get_world_group,
    get_runtime_state,
    initialize_runtime_state,
    is_dp_last_group,
    get_sequence_parallel_rank,
)
from xfuser.core.fast_attention import (
    get_fast_attn_enable,
    initialize_fast_attn_state,
    fast_attention_compression,
)
from xfuser.model_executor.base_wrapper import xFuserBaseWrapper

from xfuser.envs import PACKAGES_CHECKER

PACKAGES_CHECKER.check_diffusers_version()

from xfuser.model_executor.schedulers import *
from xfuser.model_executor.models.transformers import *
from xfuser.model_executor.layers.attention_processor import *

try:
    import os
    from onediff.infer_compiler import compile as od_compile

    HAS_OF = True
    os.environ["NEXFORT_FUSE_TIMESTEP_EMBEDDING"] = "0"
    os.environ["NEXFORT_FX_FORCE_TRITON_SDPA"] = "1"
except:
    HAS_OF = False

logger = init_logger(__name__)


class xFuserPipelineBaseWrapper(xFuserBaseWrapper, metaclass=ABCMeta):

    def __init__(
        self,
        pipeline: DiffusionPipeline,
        engine_config: EngineConfig,
    ):
        self.module: DiffusionPipeline
        self._init_runtime_state(pipeline=pipeline, engine_config=engine_config)
        self._init_fast_attn_state(pipeline=pipeline, engine_config=engine_config)

        # backbone
        transformer = getattr(pipeline, "transformer", None)
        unet = getattr(pipeline, "unet", None)
        # vae
        vae = getattr(pipeline, "vae", None)
        # scheduler
        scheduler = getattr(pipeline, "scheduler", None)

        if transformer is not None:
            pipeline.transformer = self._convert_transformer_backbone(
                transformer,
                enable_torch_compile=engine_config.runtime_config.use_torch_compile,
                enable_onediff=engine_config.runtime_config.use_onediff,
            )
        elif unet is not None:
            pipeline.unet = self._convert_unet_backbone(unet)

        if scheduler is not None:
            pipeline.scheduler = self._convert_scheduler(scheduler)

        if vae is not None and engine_config.runtime_config.use_parallel_vae and not self.use_naive_forward():
            pipeline.vae = self._convert_vae(vae)

        super().__init__(module=pipeline)

    def reset_activation_cache(self):
        if hasattr(self.module, "transformer") and hasattr(
            self.module.transformer, "reset_activation_cache"
        ):
            self.module.transformer.reset_activation_cache()
        if hasattr(self.module, "unet") and hasattr(
            self.module.unet, "reset_activation_cache"
        ):
            self.module.unet.reset_activation_cache()
        if hasattr(self.module, "vae") and hasattr(
            self.module.vae, "reset_activation_cache"
        ):
            self.module.vae.reset_activation_cache()
        if hasattr(self.module, "scheduler") and hasattr(
            self.module.scheduler, "reset_activation_cache"
        ):
            self.module.scheduler.reset_activation_cache()

    def to(self, *args, **kwargs):
        self.module = self.module.to(*args, **kwargs)
        return self

    @staticmethod
    def enable_fast_attn(func):
        @wraps(func)
        def fast_attn_fn(self, *args, **kwargs):
            if get_fast_attn_enable():
                for block in self.module.transformer.transformer_blocks:
                    for layer in block.children():
                        if isinstance(layer, xFuserAttentionBaseWrapper):
                            layer.stepi = 0
                            layer.cached_residual = None
                            layer.cached_output = None
                out = func(self, *args, **kwargs)
                for block in self.module.transformer.transformer_blocks:
                    for layer in block.children():
                        if isinstance(layer, xFuserAttentionBaseWrapper):
                            layer.stepi = 0
                            layer.cached_residual = None
                            layer.cached_output = None
                return out
            else:
                return func(self, *args, **kwargs)

        return fast_attn_fn

    @staticmethod
    def enable_data_parallel(func):
        @wraps(func)
        def data_parallel_fn(self, *args, **kwargs):
            prompt = kwargs.get("prompt", None)
            negative_prompt = kwargs.get("negative_prompt", "")
            # dp_degree <= batch_size
            batch_size = len(prompt) if isinstance(prompt, list) else 1
            if batch_size > 1:
                dp_degree = get_runtime_state().parallel_config.dp_degree
                dp_group_rank = get_world_group().rank // (
                    get_world_group().world_size // get_data_parallel_world_size()
                )
                dp_group_batch_size = (batch_size + dp_degree - 1) // dp_degree
                start_batch_idx = dp_group_rank * dp_group_batch_size
                end_batch_idx = min(
                    (dp_group_rank + 1) * dp_group_batch_size, batch_size
                )
                prompt = prompt[start_batch_idx:end_batch_idx]
                if isinstance(negative_prompt, List):
                    negative_prompt = negative_prompt[start_batch_idx:end_batch_idx]
                kwargs["prompt"] = prompt
                if "negative_prompt" in kwargs:
                    kwargs["negative_prompt"] = negative_prompt
            return func(self, *args, **kwargs)

        return data_parallel_fn

    def use_naive_forward(self):
        return (
                get_pipeline_parallel_world_size() == 1
                and get_classifier_free_guidance_world_size() == 1
                and get_sequence_parallel_world_size() == 1
                and get_tensor_model_parallel_world_size() == 1
                and get_fast_attn_enable() == False
            )
        
    @staticmethod
    def check_to_use_naive_forward(func):
        @wraps(func)
        def check_naive_forward_fn(self, *args, **kwargs):
            if self.use_naive_forward():
                return self.module(*args, **kwargs)
            else:
                return func(self, *args, **kwargs)

        return check_naive_forward_fn

    @staticmethod
    def check_model_parallel_state(
        cfg_parallel_available: bool = True,
        sequence_parallel_available: bool = True,
        pipefusion_parallel_available: bool = True,
    ):
        def decorator(func):
            @wraps(func)
            def wrapper(*args, **kwargs):
                if (
                    not cfg_parallel_available
                    and get_runtime_state().parallel_config.cfg_degree > 1
                ):
                    raise RuntimeError("CFG parallelism is not supported by the model")
                if (
                    not sequence_parallel_available
                    and get_runtime_state().parallel_config.sp_degree > 1
                ):
                    raise RuntimeError(
                        "Sequence parallelism is not supported by the model"
                    )
                if (
                    not pipefusion_parallel_available
                    and get_runtime_state().parallel_config.pp_degree > 1
                ):
                    raise RuntimeError(
                        "Pipefusion parallelism is not supported by the model"
                    )
                return func(*args, **kwargs)

            return wrapper

        return decorator

    def forward(self):
        pass

    def prepare_run(
        self, input_config: InputConfig, steps: int = 3, sync_steps: int = 1
    ):
        if get_fast_attn_enable():
            # set compression methods for DiTFastAttn
            fast_attention_compression(self)

        prompt = [""] * input_config.batch_size if input_config.batch_size > 1 else ""
        warmup_steps = get_runtime_state().runtime_config.warmup_steps
        get_runtime_state().runtime_config.warmup_steps = sync_steps
        self.__call__(
            height=input_config.height,
            width=input_config.width,
            prompt=prompt,
            use_resolution_binning=input_config.use_resolution_binning,
            num_inference_steps=steps,
            generator=torch.Generator(device="cuda").manual_seed(42),
            output_type=input_config.output_type,
        )
        get_runtime_state().runtime_config.warmup_steps = warmup_steps

    def latte_prepare_run(
        self, input_config: InputConfig, steps: int = 3, sync_steps: int = 1
    ):
        prompt = [""] * input_config.batch_size if input_config.batch_size > 1 else ""
        warmup_steps = get_runtime_state().runtime_config.warmup_steps
        get_runtime_state().runtime_config.warmup_steps = sync_steps
        self.__call__(
            height=input_config.height,
            width=input_config.width,
            prompt=prompt,
            # use_resolution_binning=input_config.use_resolution_binning,
            num_inference_steps=steps,
            output_type="latent",
            generator=torch.Generator(device="cuda").manual_seed(42),
        )
        get_runtime_state().runtime_config.warmup_steps = warmup_steps

    def _init_runtime_state(
        self, pipeline: DiffusionPipeline, engine_config: EngineConfig
    ):
        initialize_runtime_state(pipeline=pipeline, engine_config=engine_config)

    def _init_fast_attn_state(
        self, pipeline: DiffusionPipeline, engine_config: EngineConfig
    ):
        initialize_fast_attn_state(pipeline=pipeline, single_config=engine_config.fast_attn_config)

    def _convert_transformer_backbone(
        self, transformer: nn.Module, enable_torch_compile: bool, enable_onediff: bool
    ):
        if (
            get_pipeline_parallel_world_size() == 1
            and get_sequence_parallel_world_size() == 1
            and get_classifier_free_guidance_world_size() == 1
            and get_tensor_model_parallel_world_size() == 1
            and get_fast_attn_enable() == False
        ):
            logger.info(
                "Transformer backbone found, but model parallelism is not enabled, "
                "use naive model"
            )
        else:
            logger.info("Transformer backbone found, paralleling transformer...")
            wrapper = xFuserTransformerWrappersRegister.get_wrapper(transformer)
            transformer = wrapper(transformer)

        if enable_torch_compile and enable_onediff:
            logger.warning(
                f"apply --use_torch_compile and --use_onediff togather. we use torch compile only"
            )

        if enable_torch_compile or enable_onediff:
            if getattr(transformer, "forward") is not None:
                if enable_torch_compile:
                    optimized_transformer_forward = torch.compile(
                        getattr(transformer, "forward")
                    )
                elif enable_onediff:
                    # O3: +fp16 reduction
                    if not HAS_OF:
                        raise RuntimeError(
                            "install onediff and nexfort to --use_onediff"
                        )
                    options = {"mode": "O3"}  # mode can be O2 or O3
                    optimized_transformer_forward = od_compile(
                        getattr(transformer, "forward"),
                        backend="nexfort",
                        options=options,
                    )
                setattr(transformer, "forward", optimized_transformer_forward)
            else:
                raise AttributeError(
                    f"Transformer backbone type: {transformer.__class__.__name__} has no attribute 'forward'"
                )
        return transformer

    def _convert_unet_backbone(
        self,
        unet: nn.Module,
    ):
        logger.info("UNet Backbone found")
        raise NotImplementedError("UNet parallelisation is not supported yet")

    def _convert_scheduler(
        self,
        scheduler: nn.Module,
    ):
        logger.info("Scheduler found, paralleling scheduler...")
        wrapper = xFuserSchedulerWrappersRegister.get_wrapper(scheduler)
        scheduler = wrapper(scheduler)
        return scheduler

    def _convert_vae(
        self,
        vae: AutoencoderKL,
    ):
        logger.info("VAE found, paralleling vae...")
        vae.decoder = DecoderAdapter(vae.decoder)
        return vae

    @abstractmethod
    def __call__(self):
        pass

    def _init_sync_pipeline(self, latents: torch.Tensor):
        get_runtime_state().set_patched_mode(patch_mode=False)

        latents_list = [
            latents[:, :, start_idx:end_idx, :]
            for start_idx, end_idx in get_runtime_state().pp_patches_start_end_idx_global
        ]
        latents = torch.cat(latents_list, dim=-2)
        return latents

    def _init_video_sync_pipeline(self, latents: torch.Tensor):
        get_runtime_state().set_patched_mode(patch_mode=False)
        latents_list = [
            latents[:, :, :, start_idx:end_idx, :]
            for start_idx, end_idx in get_runtime_state().pp_patches_start_end_idx_global
        ]
        latents = torch.cat(latents_list, dim=-2)
        return latents

    def _init_async_pipeline(
        self,
        num_timesteps: int,
        latents: torch.Tensor,
        num_pipeline_warmup_steps: int,
    ):
        get_runtime_state().set_patched_mode(patch_mode=True)

        if is_pipeline_first_stage():
            # get latents computed in warmup stage
            # ignore latents after the last timestep
            latents = (
                get_pp_group().pipeline_recv()
                if num_pipeline_warmup_steps > 0
                else latents
            )
            patch_latents = list(
                latents.split(get_runtime_state().pp_patches_height, dim=2)
            )
        elif is_pipeline_last_stage():
            patch_latents = list(
                latents.split(get_runtime_state().pp_patches_height, dim=2)
            )
        else:
            patch_latents = [
                None for _ in range(get_runtime_state().num_pipeline_patch)
            ]

        recv_timesteps = (
            num_timesteps - 1 if is_pipeline_first_stage() else num_timesteps
        )
        for _ in range(recv_timesteps):
            for patch_idx in range(get_runtime_state().num_pipeline_patch):
                get_pp_group().add_pipeline_recv_task(patch_idx)

        return patch_latents

    def _process_cfg_split_batch(
        self,
        concat_group_0_negative: torch.Tensor,
        concat_group_0: torch.Tensor,
        concat_group_1_negative: torch.Tensor,
        concat_group_1: torch.Tensor,
    ):
        if get_classifier_free_guidance_world_size() == 1:
            concat_group_0 = torch.cat([concat_group_0_negative, concat_group_0], dim=0)
            concat_group_1 = torch.cat([concat_group_1_negative, concat_group_1], dim=0)
        elif get_classifier_free_guidance_rank() == 0:
            concat_group_0 = concat_group_0_negative
            concat_group_1 = concat_group_1_negative
        elif get_classifier_free_guidance_rank() == 1:
            concat_group_0 = concat_group_0
            concat_group_1 = concat_group_1
        else:
            raise ValueError("Invalid classifier free guidance rank")
        return concat_group_0, concat_group_1

    def _process_cfg_split_batch_latte(
        self,
        concat_group_0: torch.Tensor,
        concat_group_0_negative: torch.Tensor,
    ):
        if get_classifier_free_guidance_world_size() == 1:
            concat_group_0 = torch.cat([concat_group_0_negative, concat_group_0], dim=0)
        elif get_classifier_free_guidance_rank() == 0:
            concat_group_0 = concat_group_0_negative
        elif get_classifier_free_guidance_rank() == 1:
            concat_group_0 = concat_group_0
        else:
            raise ValueError("Invalid classifier free guidance rank")
        return concat_group_0
    
    def is_dp_last_group(self):
        """Return True if in the last data parallel group, False otherwise.
        Also include parallel vae situation.
        """
        if get_runtime_state().runtime_config.use_parallel_vae and not self.use_naive_forward():
            return get_world_group().rank == 0
        else:
            return is_dp_last_group()
    
    def gather_broadcast_latents(self, latents:torch.Tensor):
        """gather latents from dp last group and broacast final latents
        """
        
        # ---------gather latents from dp last group-----------
        rank = get_world_group().rank
        device = f"cuda:{rank}"

        # all gather dp last group rank list
        dp_rank_list = [torch.zeros(1, dtype=int, device=device) for _ in range(get_world_group().world_size)]
        if is_dp_last_group():
            gather_rank = int(rank)
        else:
            gather_rank = -1
        torch.distributed.all_gather(dp_rank_list, torch.tensor([gather_rank],dtype=int,device=device))
        
        dp_rank_list = [int(dp_rank[0]) for dp_rank in dp_rank_list if int(dp_rank[0])!=-1]
        dp_last_group = torch.distributed.new_group(dp_rank_list)

        # gather latents from dp last group
        if rank == dp_rank_list[-1]:
            latents_list = [torch.zeros_like(latents) for _ in dp_rank_list]
        else:
            latents_list = None
        if rank in dp_rank_list:
            torch.distributed.gather(latents, latents_list, dst=dp_rank_list[-1], group=dp_last_group)

        if rank == dp_rank_list[-1]:
            latents = torch.cat(latents_list,dim=0)
        
        # ------broadcast latents to all nodes---------
        src = dp_rank_list[-1]
        latents_shape_len = torch.zeros(1,dtype=torch.int,device=device)
        
        # broadcast latents shape len
        if rank == src:
            latents_shape_len[0] = len(latents.shape)
        get_world_group().broadcast(latents_shape_len,src=src)
        
        # broadcast latents shape
        if rank == src:
            input_shape = torch.tensor(latents.shape,dtype=torch.int,device=device)
        else:
            input_shape = torch.zeros(latents_shape_len[0],dtype=torch.int,device=device)
        get_world_group().broadcast(input_shape,src=src)
        
        # broadcast latents
        if rank != src:
            dtype = get_runtime_state().runtime_config.dtype
            latents = torch.zeros(torch.Size(input_shape),dtype=dtype,device=device)
        get_world_group().broadcast(latents,src=src)

        return latents
