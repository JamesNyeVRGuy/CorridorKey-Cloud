"""PyInstaller runtime hook — redirect rocm_sdk DLL loading to the bundle.

In a normal Python install, torch calls rocm_sdk.initialize_process() which
preloads HIP/ROCm DLLs from _rocm_sdk_core/bin/ and _rocm_sdk_libraries_custom/bin/.

In a frozen build, those package directories don't exist. The spec file copies
the DLLs into torch/lib/ at build time. This hook patches rocm_sdk.find_libraries()
to return paths from torch/lib/ instead, so initialize_process() works normally.
"""

import os
import sys

if getattr(sys, "frozen", False):
    # In frozen builds, torch/lib/ is at _internal/torch/lib/
    _torch_lib = os.path.join(sys._MEIPASS, "torch", "lib")

    try:
        import rocm_sdk

        _orig_find = rocm_sdk.find_libraries

        def _frozen_find_libraries(*shortnames):
            """Find ROCm DLLs in the PyInstaller bundle's torch/lib/ directory."""
            from pathlib import Path
            from rocm_sdk._dist_info import ALL_LIBRARIES

            paths = []
            for shortname in shortnames:
                lib_entry = ALL_LIBRARIES.get(shortname)
                if lib_entry is None:
                    continue
                pattern = lib_entry.dll_pattern if sys.platform == "win32" else lib_entry.so_pattern
                if not pattern:
                    continue
                matches = sorted(Path(_torch_lib).glob(pattern))
                if matches:
                    paths.append(matches[0])
            return paths

        rocm_sdk.find_libraries = _frozen_find_libraries

    except ImportError:
        pass
