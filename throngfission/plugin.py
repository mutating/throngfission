from pathlib import Path
from typing import List, Optional, Union

from throng import throng

from throngfission.manager import FissionManager


@throng.plugin(unique=True)
def fission(path: Union[str, Path] = '.', exclude: Optional[List[str]] = None) -> FissionManager:
    return FissionManager(path, exclude)
