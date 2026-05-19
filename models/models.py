from typing import Dict, Any, List, Optional
from dataclasses import dataclass, field, fields
import json

@dataclass
class CaseContext:
    ##----- case info -----##
    case_nbr: Optional[str] = None
    id: Optional[str] = None
    backend_id: Optional[str] = ""
    
    subject: Optional[str] = None
    description: Optional[str] = None
    env_detail: Optional[Dict] = field(default_factory=dict)
    subcategory: Optional[str] = None

    comments: Optional[str | List[str]] = None
    attachment_info: Optional[Dict] = field(default_factory=dict)
    attachment_list: Optional[List[Any]] = field(default_factory=list)

    ##----- case utils -----##
    case_download_dir: Optional[str] = None
    ips_pdf_path: Optional[str] = None
    error_message: Optional[str] = None

    ##----- case attribute -----##
    wifi_or_bt: Optional[str] = None

    ##----- helper functions -----##
    def print_all(self):
        print(f"\n=== {self.__class__.__name__} ===")
        for field in fields(self):
            value = getattr(self, field.name)
            print(f"{field.name:20}: {value}")
        print("=" * 30)

    def to_dict(self):
        return {field.name: getattr(self, field.name) for field in fields(self)}
    
    def to_json(self, indent=2):
        return json.dumps(self.to_dict(), indent=indent, ensure_ascii=False)
    
    # ── Heavy fields that can blow past Flask's 4 KB cookie session ──
    # Cases with many comments or attachments (e.g. 00984509) easily
    # push case_context past the cookie limit, causing the browser to
    # silently drop the cookie and the next request to throw
    # `KeyError: 'case_context'`. We stash these fields to disk and
    # keep only a sidecar JSON path in the cookie session.
    _HEAVY_SESSION_FIELDS = ("comments", "attachment_info", "attachment_list")
    _SESSION_SIDECAR_NAME = ".case_context_session.json"

    # Reuse Flask's TaggedJSONSerializer so the on-disk sidecar
    # behaves identically to the cookie session: datetime, UUID, tuple
    # etc. are tagged on write and decoded back on read — vanilla
    # json.dump() can't serialise comments[0] (a datetime).
    @staticmethod
    def _heavy_serializer():
        from flask.json.tag import TaggedJSONSerializer
        return TaggedJSONSerializer()

    def to_session(self):
        """
        Pack for Flask's cookie session. Heavy fields are written to
        <case_download_dir>/.case_context_session.json so the cookie
        stays well under the 4 KB browser limit; the rest goes in the
        cookie unchanged. Falls back to in-cookie packing when the
        download dir isn't available yet or the sidecar write fails.
        """
        full = self.to_dict()
        if not self.case_download_dir:
            return full
        try:
            import os
            os.makedirs(self.case_download_dir, exist_ok=True)
            sidecar = os.path.join(self.case_download_dir, self._SESSION_SIDECAR_NAME)
            heavy = {k: full.get(k) for k in self._HEAVY_SESSION_FIELDS}
            serialized = self._heavy_serializer().dumps(heavy)
            with open(sidecar, "w", encoding="utf-8") as f:
                f.write(serialized)
            slim = {k: v for k, v in full.items() if k not in self._HEAVY_SESSION_FIELDS}
            slim["_heavy_sidecar"] = sidecar
            return slim
        except Exception as e:
            # If the sidecar write fails for any reason, fall back to
            # the full payload so we never break the flow — the worst
            # case is the cookie-too-large warning on truly heavy cases.
            print(f"[CaseContext] sidecar stash failed ({e}); falling back to in-cookie session")
            return full

    @classmethod
    def from_session(cls, data):
        if not data:
            return cls()
        # CRITICAL: work on a COPY of the input dict. Flask's session
        # object holds the dict BY REFERENCE — if we merge heavy fields
        # back into `data` directly, the session dict itself fattens
        # up and the NEXT response writes the full payload into the
        # cookie again (defeating the whole sidecar). The copy keeps
        # the rehydration scoped to the CaseContext we return.
        if isinstance(data, dict):
            data = dict(data)
        # Rehydrate heavy fields from the sidecar file if the slim
        # session payload pointed to one. Missing/unreadable sidecar
        # falls back to empty defaults — the caller still gets a usable
        # CaseContext with all light fields intact.
        if isinstance(data, dict) and data.get("_heavy_sidecar"):
            sidecar = data.pop("_heavy_sidecar")
            try:
                import os
                if os.path.exists(sidecar):
                    with open(sidecar, "r", encoding="utf-8") as f:
                        heavy = cls._heavy_serializer().loads(f.read())
                    for k in cls._HEAVY_SESSION_FIELDS:
                        if k not in data and k in heavy:
                            data[k] = heavy[k]
            except Exception as e:
                print(f"[CaseContext] sidecar load failed ({sidecar}): {e}")
        # Drop any stray keys that aren't real CaseContext fields so
        # cls(**data) doesn't raise on legacy/extra payloads.
        valid = {f.name for f in fields(cls)}
        return cls(**{k: v for k, v in data.items() if k in valid})
    
    