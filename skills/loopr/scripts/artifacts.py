"""Private deterministic artifact writes."""
from __future__ import annotations
import json, os, stat, uuid
from pathlib import Path
from typing import Any
from models import EXIT_PRECONDITION, LooprError
from process import CommandRunner
class ArtifactWriter:
    def __init__(self,root:Path,runner:CommandRunner)->None:
        self.root=root.resolve(); self.runner=runner
        self.root.mkdir(mode=0o700,parents=True,exist_ok=True)
        meta=self.root.lstat()
        if not stat.S_ISDIR(meta.st_mode) or self.root.is_symlink() or meta.st_mode & 0o077:
            raise LooprError(EXIT_PRECONDITION,"artifacts","artifact directory must be a private real directory")
    def _path(self,relative:str)->Path:
        path=self.root/relative
        try: path.parent.resolve().relative_to(self.root)
        except ValueError as exc: raise LooprError(EXIT_PRECONDITION,"artifacts","artifact path escaped root") from exc
        return path
    def text(self,relative:str,value:str)->Path:
        path=self._path(relative); path.parent.mkdir(mode=0o700,parents=True,exist_ok=True)
        safe=self.runner.redact(value); tmp=path.with_name(f".{path.name}.{uuid.uuid4().hex}.tmp")
        fd=os.open(tmp,os.O_WRONLY|os.O_CREAT|os.O_EXCL|getattr(os,"O_NOFOLLOW",0),0o600)
        try:
            with os.fdopen(fd,"w",encoding="utf-8") as handle:
                handle.write(safe); handle.flush(); os.fsync(handle.fileno())
            os.replace(tmp,path)
        finally:
            if tmp.exists(): tmp.unlink()
        return path
    def json(self,relative:str,value:Any)->Path:
        return self.text(relative,json.dumps(value,sort_keys=True,indent=2,ensure_ascii=False)+"\n")
