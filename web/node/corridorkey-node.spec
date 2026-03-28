# -*- mode: python ; coding: utf-8 -*-
"""PyInstaller spec for the CorridorKey node agent.

Build (from repo root):
    pyinstaller web/node/corridorkey-node.spec

Output: dist/corridorkey-node/ (--onedir, ~500-700MB with CPU torch)

The binary ships with CPU-only torch. On first launch the node detects
the GPU vendor and downloads the appropriate CUDA or ROCm torch addon.
Model weights are also downloaded on first launch.
"""

import sys
from pathlib import Path

from PyInstaller.utils.hooks import collect_submodules

block_cipher = None

# Repo root (spec file is at web/node/corridorkey-node.spec → up 2 levels)
ROOT = Path(SPECPATH).parent.parent

# Collect all submodules for packages that use dynamic imports
_hidden = (
    collect_submodules("httpx")
    + collect_submodules("httpcore")
    + collect_submodules("anyio")
    + collect_submodules("h11")
    + collect_submodules("sniffio")
    + collect_submodules("certifi")
)

# Collect HIP/ROCm DLLs that PyInstaller's hook misses.
# These live in rocm_sdk_core/ and rocm_sdk_libraries_custom/, not torch/lib/.
import glob as _glob
import importlib

_extra_binaries = []
_hip_patterns = ['*hip*', '*rocm*', '*miopen*', '*hiprtc*', '*amdhip*', '*rocsolver*',
                 '*rocblas*', '*hipblas*', '*hsa*', '*amd_comgr*', '*rocfft*', '*rocsparse*',
                 'c10_hip*', 'torch_hip*']

# Search torch/lib
_torch_lib = os.path.join(os.path.dirname(__import__('torch').__file__), 'lib')
for pattern in _hip_patterns:
    for f in _glob.glob(os.path.join(_torch_lib, pattern)):
        _extra_binaries.append((f, 'torch/lib'))

# Search rocm SDK packages (rocm_sdk_core, rocm_sdk_libraries_custom, etc.)
for pkg_name in ['rocm_sdk_core', 'rocm_sdk_libraries_custom', 'rocm_sdk_devel']:
    try:
        pkg = importlib.import_module(pkg_name)
        pkg_dir = os.path.dirname(pkg.__file__)
        for f in _glob.glob(os.path.join(pkg_dir, '**', '*.dll'), recursive=True):
            _extra_binaries.append((f, 'torch/lib'))
        for f in _glob.glob(os.path.join(pkg_dir, '**', '*.so'), recursive=True):
            _extra_binaries.append((f, 'torch/lib'))
    except ImportError:
        pass

if _extra_binaries:
    print(f"[corridorkey-node.spec] Collected {len(_extra_binaries)} extra HIP/ROCm binaries")

a = Analysis(
    [str(ROOT / "web" / "node" / "corridorkey_node_main.py")],
    pathex=[str(ROOT)],
    binaries=_extra_binaries,
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
    ),
    hiddenimports=_hidden
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

# Also strip onnxruntime if it got pulled in (not needed for inference)
a.binaries = [(name, path, typ) for name, path, typ in a.binaries if "onnxruntime" not in name]

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
