# Support

* If you like my work, you can support me through https://ko-fi.com/DualTachyon

# at168-flasher.py

This lets you flash raw (custom) firmware to the AnyTone D168UV. It might work with other AnyTone models, but not tested. Base address is assumed to be 0x08004000. It was tested on Windows only.

# Running from source

Requires Python 3.9+.

    pip install -r requirements.txt
    python at168-flasher-gui.py

# Building a standalone executable

The GUI can be packaged into a single self-contained executable with
[PyInstaller](https://pyinstaller.org). PyInstaller cannot cross-compile, so
run it on the OS you want to build for:

    pip install -r requirements.txt pyinstaller
    pyinstaller at168-flasher.spec

The result appears in `dist/` (`AT168-Flasher` on Linux/macOS,
`AT168-Flasher.exe` on Windows).

# Releases

Pushing a version tag builds all three platforms in GitHub Actions and
attaches the binaries to a GitHub Release:

    git tag v1.0.0
    git push origin v1.0.0

Every push to `main` also builds the three executables and uploads them as CI
artifacts (see the **Actions** tab). Note: macOS and Windows binaries are
unsigned, so users may need to bypass Gatekeeper / SmartScreen on first launch.

# License

Copyright 2025 Dual Tachyon
https://github.com/DualTachyon

Licensed under the Apache License, Version 2.0 (the "License");
you may not use this file except in compliance with the License.
You may obtain a copy of the License at

    http://www.apache.org/licenses/LICENSE-2.0

    Unless required by applicable law or agreed to in writing, software
    distributed under the License is distributed on an "AS IS" BASIS,
    WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
    See the License for the specific language governing permissions and
    limitations under the License.

