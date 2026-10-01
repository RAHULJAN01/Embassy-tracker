#!/usr/bin/env python3
"""Build the password-gated public register page.
Reads site_data.json (the crawler's output), encrypts it with SITE_PASSWORD
(PBKDF2-SHA256 -> AES-256-GCM), injects the ciphertext into directory_template.html,
and writes public/index.html for GitHub Pages. The data is NEVER published in clear."""
import os, json, base64
from cryptography.hazmat.primitives.kdf.pbkdf2 import PBKDF2HMAC
from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.ciphers.aead import AESGCM

HERE = os.path.dirname(os.path.abspath(__file__))
ITER = 150000

def main():
    pw = os.environ.get("SITE_PASSWORD", "")
    if not pw:
        raise SystemExit("SITE_PASSWORD not set — refusing to publish unprotected data.")
    data = json.load(open(os.path.join(HERE, "site_data.json"), encoding="utf-8"))
    plaintext = json.dumps(data, ensure_ascii=False).encode("utf-8")

    salt, iv = os.urandom(16), os.urandom(12)
    key = PBKDF2HMAC(algorithm=hashes.SHA256(), length=32, salt=salt, iterations=ITER).derive(pw.encode("utf-8"))
    ct = AESGCM(key).encrypt(iv, plaintext, None)   # ciphertext||tag — matches WebCrypto AES-GCM
    enc = {"salt": base64.b64encode(salt).decode(), "iv": base64.b64encode(iv).decode(),
           "ct": base64.b64encode(ct).decode(), "iter": ITER}

    tpl = open(os.path.join(HERE, "directory_template.html"), encoding="utf-8").read()
    out = tpl.replace("/*__ENC__*/ null", json.dumps(enc))
    os.makedirs(os.path.join(HERE, "public"), exist_ok=True)
    with open(os.path.join(HERE, "public", "index.html"), "w", encoding="utf-8") as f:
        f.write(out)
    print(f"built public/index.html ({len(out)} bytes), {len(data.get('solicitations',[]))} records encrypted")

if __name__ == "__main__":
    main()
