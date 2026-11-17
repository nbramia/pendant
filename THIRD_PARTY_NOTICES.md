# Third-party notices

`pendant_direct/decode.py`, `flash_page.py`, `crypto.py`, `oggopus.py` and the protobuf definitions in `proto/` (and their generated code in `pendant_direct/proto/`) are adapted from [MAkcanca/pendant-cli](https://github.com/MAkcanca/pendant-cli), used under the MIT License:

```
MIT License

Copyright (c) 2026 Mustafa Akcanca

Permission is hereby granted, free of charge, to any person obtaining a copy
of this software and associated documentation files (the "Software"), to deal
in the Software without restriction, including without limitation the rights
to use, copy, modify, merge, publish, distribute, sublicense, and/or sell
copies of the Software, and to permit persons to whom the Software is
furnished to do so, subject to the following conditions:

The above copyright notice and this permission notice shall be included in all
copies or substantial portions of the Software.

THE SOFTWARE IS PROVIDED "AS IS", WITHOUT WARRANTY OF ANY KIND, EXPRESS OR
IMPLIED, INCLUDING BUT NOT LIMITED TO THE WARRANTIES OF MERCHANTABILITY,
FITNESS FOR A PARTICULAR PURPOSE AND NONINFRINGEMENT. IN NO EVENT SHALL THE
AUTHORS OR COPYRIGHT HOLDERS BE LIABLE FOR ANY CLAIM, DAMAGES OR OTHER
LIABILITY, WHETHER IN AN ACTION OF CONTRACT, TORT OR OTHERWISE, ARISING FROM,
OUT OF OR IN CONNECTION WITH THE SOFTWARE OR THE USE OR OTHER DEALINGS IN THE
SOFTWARE.

```

The BLE protocol handling follows the Limitless connector in [BasedHardware/omi](https://github.com/BasedHardware/omi) as the reference implementation.

## Speaker embedding models (downloaded at setup, not in this repository)

The optional `speakers.embedder` models `wespeaker-resnet293` and `wespeaker-resnet34` are the
WeSpeaker ResNet293-LM and ResNet34-LM speaker models trained on VoxCeleb, from
[wenet-e2e/wespeaker](https://github.com/wenet-e2e/wespeaker), in the ONNX exports published by
[k2-fsa/sherpa-onnx](https://github.com/k2-fsa/sherpa-onnx/releases/tag/speaker-recongition-models).
`python -m pendant_direct.embedders --fetch NAME` downloads them into the data folder and checks their
SHA-256. The WeSpeaker VoxCeleb models follow the license of the VoxCeleb data they were trained on,
[Creative Commons Attribution 4.0 International (CC BY 4.0)](https://creativecommons.org/licenses/by/4.0/).
Attribution: WeSpeaker, H. Wang et al., "Wespeaker: A research and production oriented speaker
embedding learning toolkit", ICASSP 2023; VoxCeleb, A. Nagrani, J. S. Chung, A. Zisserman et al.
