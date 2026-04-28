# -*- mode: python ; coding: utf-8 -*-
"""PyInstaller spec for the CorridorKey node agent.

Build (from repo root):
    pyinstaller web/node/corridorkey-node.spec

Output: dist/corridorkey-node/ (--onedir)

Bundle contents:
- NVIDIA build: bundled with CUDA torch (release-node.yml installs cu128 wheel).
- AMD build:    bundled with ROCm torch + the _rocm_sdk_core and
                _rocm_sdk_libraries_custom native packages, preserving
                their original directory layout so rocm_sdk's __file__-
                relative DLL lookup works in the frozen bundle.
                corridorkey_node_main.py registers the bin/ directories
                with os.add_dll_directory() at startup so Windows can
                resolve HIP DLLs.

Model weights are downloaded on first launch by weight_sync.py.
"""

import sys
from pathlib import Path

from PyInstaller.utils.hooks import collect_all, collect_submodules, copy_metadata

block_cipher = None

# Repo root (spec file is at web/node/corridorkey-node.spec → up 2 levels).
# SPECPATH / Analysis / PYZ / EXE / COLLECT are injected by PyInstaller at
# build time; ruff's F821 on them is suppressed via per-file-ignores in
# pyproject.toml.
ROOT = Path(SPECPATH).parent.parent

# Collect all submodules for packages that use dynamic imports
_hidden = (
    collect_submodules("httpx")
    + collect_submodules("httpcore")
    + collect_submodules("anyio")
    + collect_submodules("h11")
    + collect_submodules("sniffio")
    + collect_submodules("certifi")
    # CRKY-190: diffusers lazy-imports submodules via _LazyModule; unless we
    # eagerly collect them the first access (e.g.
    # diffusers.models.autoencoders.autoencoder_kl_temporal_decoder used by
    # GVM) raises "'NoneType' object is not iterable" because the lazy
    # loader can't find the missing submodule.
    + collect_submodules("diffusers")
    + collect_submodules("transformers")
    + collect_submodules("accelerate")
    + collect_submodules("peft")
)

# Runtime metadata lookups: transformers/huggingface-hub/diffusers call
# importlib.metadata.version(pkg) on several packages at import or first-use.
# PyInstaller doesn't bundle .dist-info by default, so copy the metadata for
# anything these libraries probe. `requests` is queried by transformers 5.x
# even though it's no longer a declared dependency, which is what was
# surfacing as "The 'requests' distribution was not found..." on nodes.
_metadata = (
    copy_metadata("transformers", recursive=True)
    + copy_metadata("huggingface-hub", recursive=True)
    + copy_metadata("diffusers", recursive=True)
    + copy_metadata("accelerate")
    + copy_metadata("peft")
    + copy_metadata("timm")
    + copy_metadata("tokenizers")
    + copy_metadata("safetensors")
    + copy_metadata("requests")
    + copy_metadata("torch")
    + copy_metadata("numpy")
    + copy_metadata("tqdm")
    + copy_metadata("packaging")
    + copy_metadata("filelock")
    + copy_metadata("imageio")
)

# Collect the rocm_sdk native packages (_rocm_sdk_core, _rocm_sdk_libraries_custom)
# preserving their original directory layout. rocm_sdk.find_libraries() resolves
# DLL paths via __file__-relative lookups against these packages, so the bundle
# must keep the package tree intact (bin/, lib/, kernel device libs, bitcode, etc.)
# rather than flattening everything into torch/lib/. Windows DLL discovery is
# bootstrapped at runtime by corridorkey_node_main.py via os.add_dll_directory().
_rocm_extras_datas = []
_rocm_extras_binaries = []
_rocm_extras_hidden = []
for _pkg_name in ["_rocm_sdk_core", "_rocm_sdk_libraries_custom"]:
    try:
        _pkg_datas, _pkg_binaries, _pkg_hidden = collect_all(_pkg_name)
        _rocm_extras_datas += _pkg_datas
        _rocm_extras_binaries += _pkg_binaries
        _rocm_extras_hidden += _pkg_hidden
    except Exception:
        pass

if _rocm_extras_binaries or _rocm_extras_datas:
    print(
        f"[corridorkey-node.spec] Collected ROCm SDK packages: "
        f"{len(_rocm_extras_binaries)} binaries, {len(_rocm_extras_datas)} data files"
    )
else:
    print("[corridorkey-node.spec] No ROCm SDK packages found (NVIDIA or CPU build)")

a = Analysis(
    [str(ROOT / "web" / "node" / "corridorkey_node_main.py")],
    pathex=[str(ROOT)],
    binaries=_rocm_extras_binaries,
    datas=[
        # App icon for tray
        (str(ROOT / "web" / "node" / "icon.png"), "web/node/"),
    ]
    + (
        [
            # Version info embedded at build time (CI writes this file)
            (str(ROOT / "web" / "node" / "_version.env"), "web/node/"),
        ]
        if (ROOT / "web" / "node" / "_version.env").exists()
        else []
    )
    + _metadata
    + _rocm_extras_datas,
    hiddenimports=_hidden
    + _rocm_extras_hidden
    + [
        # Node agent modules (relative imports not always detected)
        "web.node",
        "web.node.agent",
        "web.node.config",
        "web.node.weight_sync",
        "web.node.file_transfer",
        "web.node.log_buffer",
        "web.shared",
        "web.shared.gpu_subprocess",
        # Backend inference (lazy-loaded when processing jobs)
        "backend",
        "backend.service",
        "backend.job_queue",
        "backend.clip_state",
        "backend.frame_io",
        "backend.ffmpeg_tools",
        "backend.errors",
        "backend.validators",
        "backend.natural_sort",
        "backend.project",
        # Core model
        "CorridorKeyModule",
        "CorridorKeyModule.core",
        "CorridorKeyModule.core.color_utils",
        "CorridorKeyModule.core.model_transformer",
        "CorridorKeyModule.inference_engine",
        # Device utils
        "device_utils",
        # Alpha hint generators (lazy-loaded per job type)
        "gvm_core",
        "gvm_core.wrapper",
        "VideoMaMaInferenceModule",
        "VideoMaMaInferenceModule.inference",
        "VideoMaMaInferenceModule.pipeline",
        "BiRefNetModule",
        "BiRefNetModule.wrapper",
        # Third-party hidden imports (dynamic loading / C extensions)
        "torch._C",
        "torch._C._jit",
        "timm.models.hiera",
        "timm.layers",
        "diffusers",
        "transformers",
        "accelerate",
        "peft",
        "safetensors",
        "einops",
        "kornia",
        "certifi",
        "httpx",
        "httpx._transports",
        "httpx._transports.default",
        "httpcore",
        "h11",
        "anyio",
        "anyio._backends",
        "anyio._backends._asyncio",
        "sniffio",
        # pystray backends (platform-specific, detected at runtime)
        *(["pystray._win32"] if sys.platform == "win32" else []),
        *(["pystray._appindicator", "pystray._xorg"] if sys.platform == "linux" else []),
        # ROCm SDK Python wrappers. The native packages (_rocm_sdk_core,
        # _rocm_sdk_libraries_custom) are collected via collect_all above.
        "rocm_sdk",
        "rocm_sdk._dist_info",
    ],
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=[
        # Web server (not needed for node agent)
        "fastapi",
        "uvicorn",
        "starlette",
        "PyJWT",
        "psycopg2",
        "alembic",
        "sqlalchemy",
        "sentry_sdk",
        "boto3",
        "botocore",
        # IPython / Jupyter
        "IPython",
        "jupyter",
        "notebook",
    ],
    noarchive=False,
    optimize=0,
)

# Strip model weights — they're downloaded at runtime by weight_sync.py.
# These patterns match the multi-GB checkpoint/weight files that PyInstaller
# pulls in from the source tree.
_weight_patterns = [
    "checkpoints",
    "weights",
    ".pth",
    ".safetensors",
    ".ckpt",
    ".bin",
    "diffusion_pytorch_model",
    "dino_projection_mlp",
]
a.datas = [(name, path, typ) for name, path, typ in a.datas if not any(p in name for p in _weight_patterns)]

# Strip binaries that shouldn't be in the bundle:
# - onnxruntime: not needed for inference
# - clang_rt.asan: debug-only ASAN runtime, crashes when loaded alongside system ucrtbase
# - caffe2_nvrtc: NVIDIA DLL shipped in AMD torch wheel, crashes on AMD-only machines
a.binaries = [
    (name, path, typ)
    for name, path, typ in a.binaries
    if "onnxruntime" not in name and "clang_rt.asan" not in name and "caffe2_nvrtc" not in name
]

pyz = PYZ(a.pure, a.zipped_data, cipher=block_cipher)

exe = EXE(
    pyz,
    a.scripts,
    [],
    exclude_binaries=True,
    name="corridorkey-node",
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=False,  # UPX breaks torch .dll/.so files
    console=False,  # windowed app — no cmd prompt
    disable_windowed_traceback=False,
    argv_emulation=False,
    icon=str(ROOT / "web" / "node" / "icon.ico"),
)

coll = COLLECT(
    exe,
    a.binaries,
    a.datas,
    strip=False,
    upx=False,
    upx_exclude=[],
    name="corridorkey-node",
)
