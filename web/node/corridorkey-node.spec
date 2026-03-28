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

# Collect HIP/ROCm DLLs that PyInstaller's binary analysis misses.
# The DLLs live in _rocm_sdk_core/bin/ and _rocm_sdk_libraries_custom/bin/
# (note the underscore prefix — the non-prefixed packages are just Python wrappers).
# We copy them into torch/lib/ so they're on the DLL search path at runtime.
import glob as _glob
import importlib

_extra_binaries = []

# Search rocm SDK native packages for DLLs.
# These are the packages that contain the actual HIP/ROCm runtime libraries.
for pkg_name in ['_rocm_sdk_core', '_rocm_sdk_libraries_custom']:
    try:
        pkg = importlib.import_module(pkg_name)
        pkg_dir = os.path.dirname(pkg.__file__)
        for f in _glob.glob(os.path.join(pkg_dir, '**', '*.dll'), recursive=True):
            if 'clang_rt.asan' not in os.path.basename(f):
                _extra_binaries.append((f, 'torch/lib'))
        for f in _glob.glob(os.path.join(pkg_dir, '**', '*.so'), recursive=True):
            _extra_binaries.append((f, 'torch/lib'))
    except ImportError:
        pass

if _extra_binaries:
    print(f"[corridorkey-node.spec] Collected {len(_extra_binaries)} extra HIP/ROCm binaries")
else:
    print("[corridorkey-node.spec] No ROCm packages found (NVIDIA or CPU build)")

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
        # ROCm SDK (runtime hook patches find_libraries, needs _dist_info for DLL patterns)
        "rocm_sdk",
        "rocm_sdk._dist_info",
    ],
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[str(ROOT / "web" / "node" / "pyi_rth_rocm.py")],
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
    (name, path, typ) for name, path, typ in a.binaries
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
