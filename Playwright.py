"""Encrypted sessions and browser capture for urlwatch."""
from argparse import ArgumentParser
from contextlib import contextmanager
from hashlib import sha256
from pathlib import Path
from shutil import which
from subprocess import DEVNULL, Popen
from tempfile import TemporaryDirectory
from urllib.parse import urlsplit
import json
import os
import re
import sys
import keyring
from cryptography.fernet import Fernet
from filelock import FileLock
from platformdirs import PlatformDirs
from playwright.sync_api import Error as PlaywrightError, sync_playwright

ROOT = Path(PlatformDirs("urlwatch").user_config_dir) / "playwright"

def save(path, data):
    """Save and keep the previous file until ready."""
    path.parent.mkdir(parents=True, exist_ok=True)
    with FileLock(str(path) + ".io.lock"):
        temporary = path.with_suffix(".tmp")
        temporary.write_bytes(data if isinstance(data, bytes) else json.dumps(data).encode("utf-8"))
        temporary.replace(path)

class RateLimited(RuntimeError):
    def __init__(self, retry_after=None):
        self.retry_after = retry_after
        super().__init__(f"HTTP 429: rate limited. Retry-After: {retry_after or 'not supplied'}")

def session(url, account, browser, refresh=False):
    """Load a saved session or save a new one."""
    parts = urlsplit(url)
    if parts.scheme not in {"http", "https"} or not parts.hostname or not account.strip():
        raise ValueError("Use HTTP(S) URL and an account")
    origin = f"{parts.scheme}://{parts.netloc}"
    site = re.sub(r"[^A-Za-z0-9.-]", "_", parts.hostname)[:60] + "-" + sha256(origin.encode()).hexdigest()[:12]
    state = ROOT / "sessions" / sha256(account.encode()).hexdigest()[:16] / f"{site}.fernet"
    refresh = refresh or not state.is_file()
    service, key_id = "urlwatch-playwright", f"session-key:{state.parent.name}"
    key = keyring.get_password(service, key_id)
    if key is None and refresh and not any(state.parent.glob("*.fernet")):
        key = Fernet.generate_key().decode()
        keyring.set_password(service, key_id, key)
    if key is None:
        raise RuntimeError("Key unavailable, unlock keyring or restore key")
    cipher = Fernet(key.encode())
    if not refresh:
        return json.loads(cipher.decrypt(state.read_bytes()))
    data = sign_in(origin + "/", browser)
    if not data.get("cookies") and not data.get("origins"):
        raise RuntimeError("No session captured, the sign-in browser did not complete. Previous session kept.")
    save(state, cipher.encrypt(json.dumps(data).encode()))
    return data

def find_browser(name):
    app, directory, command = {
        "msedge": ("Microsoft Edge", "Microsoft/Edge", "microsoft-edge"),
        "chrome": ("Google Chrome", "Google/Chrome", "google-chrome"),
    }[name]
    paths = [which(command), which(command + "-stable")]
    paths += [f"{root}/{directory}/Application/{name}.exe" for variable in
              ("PROGRAMFILES", "PROGRAMFILES(X86)", "LOCALAPPDATA") if (root := os.getenv(variable))]
    paths += [f"{root}/{app}.app/Contents/MacOS/{app}"
              for root in ("/Applications", Path.home() / "Applications")]
    return next((str(path) for path in paths if path and Path(path).is_file()), None)

def sign_in(origin, browser):
    """Login via another browser and export the credentials."""
    executable = (find_browser(browser) if browser in {"chrome", "msedge"}
                  else find_browser("msedge") or find_browser("chrome"))
    if not executable:
        raise RuntimeError("Install Edge or Chrome")
    print("Sign in and close the browser window to save. Note that this temporary profile is unencrypted.", file=sys.stderr)
    with TemporaryDirectory(prefix="urlwatch-login-") as profile:
        process = Popen([executable, f"--user-data-dir={profile}", "--no-first-run",
                         "--edge-skip-compat-layer-relaunch",
                         "--no-default-browser-check", "--disable-background-mode", "--new-window", origin],
                        stdin=DEVNULL, stdout=DEVNULL, stderr=DEVNULL)
        canceled = False
        while process.poll() is None:
            try:
                process.wait()
            except KeyboardInterrupt:
                canceled = True
                print("Canceled, close browser to finish cleanup", file=sys.stderr)
        if canceled or process.returncode:
            raise RuntimeError("Sign-in canceled or browser failed")
        with sync_playwright() as p, p.chromium.launch_persistent_context(
                profile, executable_path=executable, headless=True, service_workers="block") as context:
            # Register the origin offline to export its localStorage and IndexedDB.
            context.route("**/*", lambda route: route.fulfill(content_type="text/html", body=""))
            context.new_page().goto(origin)
            return context.storage_state(indexed_db=True)

def visit(page, url):
    try:
        response = page.goto(url, wait_until="domcontentloaded")
    except PlaywrightError as error:
        if "Download is starting" not in str(error):
            raise
        raise RuntimeError("Page returned a download instead of HTML. If sign-in expired, try rerunning with --login") from None
    if response is not None and response.status == 429:
        raise RateLimited(response.headers.get("retry-after"))
    if response is None or not response.ok:
        raise RuntimeError(f"Page request failed (HTTP {response.status if response else 'unknown'})")
    if urlsplit(page.url)[:2] != urlsplit(url)[:2]:
        raise RuntimeError("Redirected to another site, if sign-in is required, run again with --login")

@contextmanager
def browser_page(url, account="default", browser="firefox", public=False, login=False):
    state = None if public else session(url, account, browser, login)
    channel = {"channel": browser} if browser in {"chrome", "msedge"} else {}
    with sync_playwright() as p, getattr(p, "chromium" if channel else browser).launch(
            headless=True, **channel) as instance:
        page = instance.new_context(storage_state=state).new_page()
        page.route("**/*", lambda r: r.abort() if r.request.resource_type in {"image", "media", "font"} else r.continue_())
        visit(page, url)
        yield page

def main():
    parser = ArgumentParser(description=__doc__)
    parser.add_argument("url")
    parser.add_argument("--account", default="default")
    parser.add_argument("--browser", default="firefox", choices=["firefox", "chromium", "webkit", "chrome", "msedge"])
    auth = parser.add_mutually_exclusive_group()
    auth.add_argument("--login", action="store_true", help="Sign in again and capture credentials")
    auth.add_argument("--no-login", action="store_true")
    parser.add_argument("--wait-for")
    args = parser.parse_args()
    with browser_page(args.url, args.account, args.browser, args.no_login, args.login) as page:
        if args.wait_for:
            page.locator(args.wait_for).first.wait_for(state="visible")
        print(page.content())

if __name__ == "__main__":
    main()
