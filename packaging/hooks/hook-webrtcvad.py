# O projeto usa a distribuição Windows webrtcvad-wheels, não webrtcvad.
from PyInstaller.utils.hooks import copy_metadata
datas = copy_metadata('webrtcvad-wheels')
hiddenimports = ['_webrtcvad']
