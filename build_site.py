#!/usr/bin/env python3
"""
build_site.py — produce the password-GATED live page.
Encrypts data.json with SITE_PASSWORD (AES-256-GCM, PBKDF2-SHA256) and injects
the ciphertext into site_template.html -> public/index.html. The password is a
GitHub Actions secret; it is never stored in the repo or seen by anyone.
If SITE_PASSWORD is unset, the page is published UNLOCKED (useful before real
data exists).
"""
import os, json, base64, secrets, pathlib
from cryptography.hazmat.primitives.kdf.pbkdf2 import PBKDF2HMAC
from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.ciphers.aead import AESGCM

HERE = pathlib.Path(__file__).parent
ITERS = 150000


def b64(b): return base64.b64encode(b).decode()


def encrypt_payload(plaintext: bytes, password: str) -> dict:
    salt = secrets.token_bytes(16)
    key = PBKDF2HMAC(algorithm=hashes.SHA256(), length=32, salt=salt, iterations=ITERS).derive(password.encode())
    iv = secrets.token_bytes(12)
    ct = AESGCM(key).encrypt(iv, plaintext, None)   # ciphertext || 16-byte tag (WebCrypto-compatible)
    return {"v": 1, "salt": b64(salt), "iv": b64(iv), "ct": b64(ct), "iter": ITERS}


def _opt(name, default):
    p = HERE / name
    try:
        return json.loads(p.read_text(encoding="utf-8"))
    except Exception:
        return default


def main():
    # bundle the directory data + Mission Control status + HELP/blocked list into ONE payload
    core = _opt("data.json", {"meta": {}, "solicitations": []})
    payload = {
        "meta": core.get("meta", {}),
        "solicitations": core.get("solicitations", []),
        "status": _opt("status.json", {}),
        "blocked": _opt("blocked.json", {"sites": []}),
        "control": _opt("control.json", {"paused": False}),
        # the operator's own delete / hide / switch decisions, and the company
        # record — both sit INSIDE the encrypted payload, so they are only
        # readable after the register password is entered
        "operator": _opt("operator.json", {"deleted": {}, "hidden": {}, "switched": {}}),
        "company": _opt("company.json", {}),
    }
    data = json.dumps(payload, ensure_ascii=False)
    template = (HERE / "site_template.html").read_text(encoding="utf-8")
    pw = os.getenv("SITE_PASSWORD", "")
    if pw:
        blob = encrypt_payload(data.encode("utf-8"), pw)
        enc_js = "const ENC=" + json.dumps(blob) + ";"
        locked = "true"
    else:
        enc_js = "const ENC=null; const PLAIN=" + data + ";"   # unlocked fallback
        locked = "false"
    out = template.replace("/*__ENC__*/", enc_js).replace("/*__LOCKED__*/", locked)
    outdir = HERE / "public"; outdir.mkdir(exist_ok=True)
    (outdir / "index.html").write_text(out, encoding="utf-8")
    print(f"built public/index.html — {'LOCKED' if pw else 'UNLOCKED'} — {len(out)} bytes")


if __name__ == "__main__":
    main()
