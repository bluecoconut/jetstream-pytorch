import dataclasses
import glob
import os
from typing import Optional
from requests.exceptions import HTTPError
from huggingface_hub import snapshot_download
from absl import flags
import torch
from safetensors import safe_open
from jetstream_pt.environment import (
    JetEngineEnvironmentData,
)
from jetstream_pt.third_party.llama import model_exportable as llama_model
from jetstream_pt.third_party.mixtral import model as mixtral_model
from jetstream_pt.third_party.gemma import model as gemma_model

FLAGS = flags.FLAGS

flags.DEFINE_string(
    "working_dir",
    "checkpoints",
    "Directory to store downloaded/converted weights",
)
flags.DEFINE_string("hf_token", "", "huggingface token")
flags.DEFINE_bool(
    "internal_use_random_weights",
    False,
    "Use random weights instead of HF weights. Testing only.",
)

flags.DEFINE_bool(
    "internal_use_tiny_model",
    False,
    "Use tiny config instead of real config of HF weights. Testing only.",
)

flags.DEFINE_integer(
    "override_max_cache_length",
    -1,
    "Size of cache, defaults to input + output length",
)


@dataclasses.dataclass
class ModelInfo:
  """Model information."""

  model_class: torch.nn.Module
  # information needed to allocate cache
  num_layers: int
  # number of kv heads
  num_kv_heads: int

  head_dim: int
  n_reps: int  # repeatition for GQA


_llama2_7 = ModelInfo(llama_model.Transformer, 32, 32, 128, 1)
_llama2_13 = ModelInfo(llama_model.Transformer, 40, 40, 128, 1)
_llama2_70 = ModelInfo(llama_model.Transformer, 80, 8, 128, 8)
_llama3_8 = ModelInfo(llama_model.Transformer, 32, 8, 128, 4)
_llama3_70 = _llama2_70
_llama3_1_8b = _llama3_8
_llama3_2_1b = ModelInfo(llama_model.Transformer, 16, 8, 64, 4)
_llama3_3_70b = _llama2_70

_mixtral_87 = ModelInfo(mixtral_model.Transformer, 32, 8, 128, 4)

_gemma_2b = ModelInfo(gemma_model.GemmaModel, 18, 1, 256, 8)
_gemma_7b = ModelInfo(gemma_model.GemmaModel, 28, 16, 256, 1)


model_id_to_class = {
    "meta-llama/Llama-2-7b-chat-hf": _llama2_7,
    "meta-llama/Llama-2-7b-hf": _llama2_7,
    "meta-llama/Llama-2-13b-chat-hf": _llama2_13,
    "meta-llama/Llama-2-13b-hf": _llama2_13,
    "meta-llama/Llama-2-70b-hf": _llama2_70,
    "meta-llama/Llama-2-70b-chat-hf": _llama2_70,
    "meta-llama/Meta-Llama-3-8B": _llama3_8,
    "meta-llama/Meta-Llama-3-8B-Instruct": _llama3_8,
    "meta-llama/Meta-Llama-3-70B": _llama3_70,
    "meta-llama/Meta-Llama-3-70B-Instruct": _llama3_70,
    "meta-llama/Llama-3.1-8B": _llama3_1_8b,
    "meta-llama/Llama-3.1-8B-Instruct": _llama3_1_8b,
    "meta-llama/Llama-3.2-1B": _llama3_2_1b,
    "meta-llama/Llama-3.2-1B-Instruct": _llama3_2_1b,
    "meta-llama/Llama-3.3-70B": _llama3_3_70b,
    "meta-llama/Llama-3.3-70B-Instruct": _llama3_3_70b,
    "google/gemma-2b": _gemma_2b,
    "google/gemma-2b-it": _gemma_2b,
    "google/gemma-7b": _gemma_7b,
    "google/gemma-7b-it": _gemma_7b,
    "mistralai/Mixtral-8x7B-v0.1": _mixtral_87,
    "mistralai/Mixtral-8x7B-Instruct-v0.1": _mixtral_87,
    "deepseek-ai/DeepSeek-R1-Distill-Llama-8B": _llama3_1_8b,
    "deepseek-ai/DeepSeek-R1-Distill-Llama-70B": _llama3_3_70b,
}


def _model_dir(repo_id):
  """Model dir structure:

  working_dir/
    repo_id/
      hf_original/
      converted_bfloat/
      converted_int8/
  """
  return os.path.join(FLAGS.working_dir, repo_id)


def _hf_dir(repo_id):
  """Dir to hf repo"""
  return os.path.join(_model_dir(repo_id), "hf_original")


def _int_dir(repo_id):
  return os.path.join(_model_dir(repo_id), "converted_int8")


def construct_env_data_from_model_id(
    repo_id,
    batch_size,
    input_length,
    output_length,
):
  """Create Environment from model id and options"""
  tokenizer_path = os.path.join(_hf_dir(repo_id), "tokenizer.model")
  checkpoint_path = _hf_dir(repo_id)
  checkpoint_format = "safetensors"

  shard_on_batch = False

  max_cache_length = (
      FLAGS.override_max_cache_length
      if FLAGS.override_max_cache_length > 0
      else input_length + output_length
  )

  model_info = model_id_to_class.get(repo_id)
  env_data = JetEngineEnvironmentData(
      tokenizer_path=tokenizer_path,
      checkpoint_path=checkpoint_path,
      checkpoint_format=checkpoint_format,
      batch_size=batch_size,
      max_decode_length=output_length,
      max_input_sequence_length=input_length,
      cache_sequence_length=max_cache_length,
      bf16_enable=True,
      sharding_config_path="",
      shard_on_batch=shard_on_batch,
      n_reps=model_info.n_reps,
  )
  env_data.cache_shape = (
      batch_size,
      model_info.num_kv_heads,
      max_cache_length,
      model_info.head_dim,
  )
  env_data.num_layers = model_info.num_layers
  return env_data


def _load_weights(directory):
  safetensors_files = glob.glob(os.path.join(directory, "*.safetensors"))
  state_dict = {}
  for file_path in safetensors_files:
    with safe_open(file_path, framework="pt") as f:
      for key in f.keys():
        state_dict[key] = f.get_tensor(key).to(torch.bfloat16)
  # Load the state_dict into the model
  if not state_dict:
    raise AssertionError(
        f"Tried to load weights from {directory}, but couldn't find any."
    )
  return state_dict


def _make_random_model_weights(model):
  result = {}
  for key, val in model.state_dict().items():
    new_weights = torch.rand(val.shape, dtype=val.dtype, device="cpu")
    result[key] = new_weights
  return result


def instantiate_model_from_repo_id(
    repo_id,
    env,
):
  """Create model instance by hf model id.+"""
  model_dir = _hf_dir(repo_id)
  if not FLAGS.internal_use_random_weights and (
      not os.path.exists(model_dir)
      or not glob.glob(os.path.join(model_dir, "*.safetensors"))
  ):
    # no weights has been downloaded
    _hf_download(repo_id, model_dir, FLAGS.hf_token)
  model_info = model_id_to_class.get(repo_id)
  assert model_info is not None

  env.device = "meta"
  model = model_info.model_class.from_hf_model_id(
      repo_id, env, FLAGS.internal_use_tiny_model
  )
  if FLAGS.internal_use_random_weights or FLAGS.internal_use_tiny_model:
    weights = _make_random_model_weights(model)
  else:
    weights = _load_weights(model_dir)
    weights = model.convert_hf_weights(weights)
  model.load_state_dict(weights, assign=True, strict=False)

  return model
  ## QQ do i need to set the weights onto the model?


def _hf_download(
    repo_id: str, dest_directory: str, hf_token: Optional[str] = None
) -> None:
  os.makedirs(dest_directory, exist_ok=True)
  try:
    if not hf_token:
      hf_token = os.environ.get("HF_TOKEN")
    if not hf_token:
      # NOTE: setting true allows hf to read from the config folder.
      hf_token = True
    snapshot_download(
        repo_id,
        local_dir=dest_directory,
        local_dir_use_symlinks=False,
        token=hf_token,
        allow_patterns=[
            "model*.safetensors",
            "*.json",
            "*.model",
        ],
    )
  except HTTPError as e:
    if e.response.status_code == 401:
      print(
          "Please use huggingface-cli login to authenticate "
          "to download private checkpoints."
      )
      print("OR, pass `hf_token=...` explicitly.")
    raise e
