"""PyInstaller runtime hook — patch rocm_sdk to skip DLL preloading.

In frozen builds, PyInstaller bundles all DLLs into _internal/ which is
already on the DLL search path. The rocm_sdk.initialize_process() function
tries to find DLLs relative to site-packages paths which don't exist in
frozen builds. This hook replaces it with a no-op.
"""

import sys

if getattr(sys, "frozen", False):
    try:
        import rocm_sdk

        # Replace the initialization with a no-op — DLLs are already bundled
        rocm_sdk.initialize_process = lambda *args, **kwargs: None
        rocm_sdk.preload_libraries = lambda *args, **kwargs: None
    except ImportError:
        pass
