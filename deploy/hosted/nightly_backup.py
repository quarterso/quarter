"""Nightly: one encrypted archive of the box, sent to every backup bucket.

Run as root on the box from /opt/quarter. The archive holds env/*.env (vendor and agent
keys), the tunnel credentials, each tenant's other files (ledger.signing-key and the like)
and a copy of each tenant's ledger made by `quarter-ledger backup`, which checks the hash
chains. It is encrypted with age to backup-recipient.txt, whose private half only the
founder holds, so no bucket holds a readable secret. It goes to secrets/<date>.tar.age in
the Litestream bucket and, when env/b2.env exists, in a second bucket off Oracle; the copy
from 30 days ago is deleted. env/healthchecks.env, when present, gets a ping on success
and a /fail ping on any error.

    python3 nightly_backup.py /opt/quarter              # the nightly run
    python3 nightly_backup.py /opt/quarter --litestream # hourly: ping if Litestream logged no error

See docs/HOSTED.md.
"""

import datetime
import hashlib
import hmac
import io
import os
import subprocess
import sys
import tarfile
import urllib.parse
import urllib.request
from pathlib import Path

KEEP_DAYS = 30
SKIP = ("ledger.db", ".ledger.db-litestream")
COPY = "nightly-backup.db"  # made inside each tenant, archived, then removed


def read_env(path):
    out = {}
    for line in Path(path).read_text().splitlines():
        if "=" in line and not line.lstrip().startswith("#"):
            k, v = line.split("=", 1)
            out[k.strip()] = v.strip()
    return out


def volumes():
    """Each tenant volume's mount point, from Docker. Names look like quarter_<tenant>."""
    names = subprocess.run(["docker", "volume", "ls", "-q", "--filter", "name=quarter_"],
                           check=True, capture_output=True, text=True).stdout.split()
    for name in names:
        if name == "quarter_sandbox":
            continue
        mount = subprocess.run(["docker", "volume", "inspect", "-f", "{{.Mountpoint}}", name],
                               check=True, capture_output=True, text=True).stdout.strip()
        yield name.removeprefix("quarter_"), Path(mount)


def snapshot(root, tenant):
    """A checked copy of the tenant's ledger, written into its own volume."""
    subprocess.run(["docker", "compose", "--project-directory", str(root), "exec", "-T", tenant,
                    "quarter-ledger", "backup", f"/data/{COPY}"], check=True, capture_output=True)


def archive(root, copies=True):
    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w:gz") as tar:
        for path in sorted((root / "env").glob("*.env")):
            tar.add(path, f"env/{path.name}")
        creds = root / "cloudflared" / "credentials.json"
        if creds.exists():
            tar.add(creds, "cloudflared/credentials.json")
        for tenant, mount in volumes():
            if copies:
                (mount / COPY).unlink(missing_ok=True)
                snapshot(root, tenant)
            try:
                for path in sorted(mount.iterdir()):
                    if path.is_file() and not path.name.startswith(SKIP):
                        tar.add(path, f"volumes/{tenant}/{path.name}")
            finally:
                (mount / COPY).unlink(missing_ok=True)
    return buf.getvalue()


def encrypt(data, recipient_file):
    return subprocess.run(["age", "-R", str(recipient_file)], input=data,
                          check=True, capture_output=True).stdout


def s3(method, env, key, body=b"", now=None):
    """One signed S3 request (AWS Signature Version 4, path style)."""
    now = now or datetime.datetime.now(datetime.UTC)
    endpoint = urllib.parse.urlparse(env["LITESTREAM_ENDPOINT"])
    region, bucket = env["LITESTREAM_REGION"], env["LITESTREAM_BUCKET"]
    path = "/" + urllib.parse.quote(f"{bucket}/{key}")
    amz_date, day = now.strftime("%Y%m%dT%H%M%SZ"), now.strftime("%Y%m%d")
    payload = hashlib.sha256(body).hexdigest()
    headers = {"host": endpoint.netloc, "x-amz-content-sha256": payload, "x-amz-date": amz_date}
    signed = ";".join(sorted(headers))
    canonical = "\n".join([method, path, "", *(f"{k}:{headers[k]}" for k in sorted(headers)), "", signed, payload])
    scope = f"{day}/{region}/s3/aws4_request"
    to_sign = "\n".join(["AWS4-HMAC-SHA256", amz_date, scope, hashlib.sha256(canonical.encode()).hexdigest()])
    k = ("AWS4" + env["LITESTREAM_SECRET_ACCESS_KEY"]).encode()
    for part in (day, region, "s3", "aws4_request"):
        k = hmac.new(k, part.encode(), hashlib.sha256).digest()
    signature = hmac.new(k, to_sign.encode(), hashlib.sha256).hexdigest()
    headers["authorization"] = (f"AWS4-HMAC-SHA256 Credential={env['LITESTREAM_ACCESS_KEY_ID']}/{scope}, "
                                f"SignedHeaders={signed}, Signature={signature}")
    req = urllib.request.Request(f"{endpoint.scheme}://{endpoint.netloc}{path}", data=body or None,
                                 method=method, headers=headers)
    try:
        with urllib.request.urlopen(req, timeout=60) as resp:
            return resp.status
    except urllib.error.HTTPError as e:
        if method == "DELETE" and e.code == 404:
            return 404
        raise


def buckets(root):
    """The Litestream bucket, and the second one if env/b2.env exists (same variable names)."""
    out = [read_env(root / "env" / "litestream.env")]
    second = root / "env" / "b2.env"
    if second.exists():
        out.append(read_env(second))
    return out


def ping(root, name, fail=False):
    hc = root / "env" / "healthchecks.env"
    url = read_env(hc).get(name) if hc.exists() else None
    if url:
        try:
            urllib.request.urlopen(url + ("/fail" if fail else ""), timeout=10).close()
        except OSError as e:
            print(f"healthchecks ping failed: {type(e).__name__}", file=sys.stderr)


def litestream_ok(root):
    logs = subprocess.run(["docker", "compose", "--project-directory", str(root), "logs", "--since", "65m",
                           "litestream"], check=True, capture_output=True, text=True).stdout
    return "level=ERROR" not in logs


def nightly(root):
    sealed = encrypt(archive(root), root / "backup-recipient.txt")
    today = datetime.date.today()
    for env in buckets(root):
        s3("PUT", env, f"secrets/{today.isoformat()}.tar.age", sealed)
        s3("DELETE", env, f"secrets/{(today - datetime.timedelta(days=KEEP_DAYS)).isoformat()}.tar.age")
        print(f"{env['LITESTREAM_BUCKET']}: secrets/{today.isoformat()}.tar.age, {len(sealed)} bytes")


def main():
    args = sys.argv[1:]
    root = Path(next((a for a in args if not a.startswith("--")), "/opt/quarter"))
    if "--litestream" in args:
        ok = litestream_ok(root)
        ping(root, "HC_LITESTREAM_URL", fail=not ok)
        print("litestream: " + ("no errors in the last 65 minutes" if ok else "errors logged"))
        sys.exit(0 if ok else 1)
    try:
        nightly(root)
    except Exception:
        ping(root, "HC_BACKUP_URL", fail=True)
        raise
    ping(root, "HC_BACKUP_URL")


if __name__ == "__main__":
    os.umask(0o077)
    main()
