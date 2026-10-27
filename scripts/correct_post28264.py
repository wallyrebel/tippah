"""One fixed reviewed post correction. Managed Actions only; no arbitrary writer."""
import base64
import copy
import hashlib
import json
import os
import sys
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

APPROVED = {'repository': 'wallyrebel/tippah', 'host': 'tippahnews.com', 'branch': 'codex/clean-post28264-20261027', 'before': {'id': 28264, 'status': 'publish', 'slug': 'the-5-best-solar-outdoor-lights-for-tippah-county-rural-driveways', 'link': 'https://tippahnews.com/mississippi-news/the-5-best-solar-outdoor-lights-for-tippah-county-rural-driveways/', 'date': '2026-10-27T06:14:50', 'date_gmt': '2026-10-27T11:14:50', 'modified_gmt': '2026-10-27T11:14:50', 'author': 1, 'featured_media': 28263, 'categories': [8], 'tags': [2939, 2948, 3355, 3354, 3353, 155], 'rendered_sha256': {'title': 'c19daec5c5b5c009736993d66f0c587b2f131d28eef1dc335b3635c0e88d8678', 'content': 'f9ea6527ec50b18e60708e8f54c144cec0f49e710e9f4b63f04e11eac91f5e4f', 'excerpt': '30606d0e755072509771f592e21627e485d4e817df1d508dd6b477b7a925400b'}}, 'payload_sha256': '2bfffa0f6e5d7bfdbad3009db4f50000774cd7e329f7f0673a7a9992aa0a4098', 'ledger_sha256': 'b44940ad600ec97d96b25c235913e6a53392fbf37a47b0580e15e2da7f19fd5e', 'operations': [{'text': '; confirm live price at publish', 'replacement': '', 'count': 2}, {'text': ' — enrich via Creators API before publishing; do not publish a guessed price', 'replacement': '', 'count': 1}], 'after_rendered_sha256': '87ad78bc3fad42b1b216cb13698913376df22c49cd477ff812c478d0716b2076'}
ROOT = Path(__file__).resolve().parents[1]
REQUEST = ROOT / "corrections" / ("post" + str(APPROVED["before"]["id"])) / "request.json"
LEDGER = REQUEST.parent / "fact-ledger.json"
DRY_RECEIPT = REQUEST.parent / "approved-dry-run.json"
OUTPUT = ROOT / "data" / ("correction-post" + str(APPROVED["before"]["id"])) / "result.json"
PRESERVE = ("id", "status", "slug", "link", "date", "date_gmt", "author",
            "featured_media", "categories", "tags", "comment_status",
            "ping_status", "sticky", "template", "format")
FIELDS = ("content",)


class GuardError(Exception):
    pass


class HTTPFailureError(Exception):
    def __init__(self, status):
        super().__init__("HTTP request failed")
        self.status = status


def require(condition, message):
    if not condition:
        raise GuardError(message)


def digest(text):
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def payload_digest(payload):
    return digest(json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":")))


def raw_hashes(post):
    return {key: digest(post[key]["raw"]) for key in ("title", "content", "excerpt")}


def rendered_hashes(post):
    return {key: digest(post[key]["rendered"]) for key in ("title", "content", "excerpt")}


def identity(post):
    return {key: copy.deepcopy(post.get(key)) for key in PRESERVE}


def desired(post, payload):
    try:
        return (digest(post["content"]["rendered"]) == APPROVED["after_rendered_sha256"]
                and all(digest(post[k]["rendered"]) == APPROVED["before"]["rendered_sha256"][k]
                        for k in ("title", "excerpt")))
    except (KeyError, TypeError):
        return False


def derive_payload(post):
    original = post["content"]["raw"]
    rendered = post["content"]["rendered"]
    for operation in APPROVED["operations"]:
        require(original.count(operation["text"]) == operation["count"],
                "Raw removal occurrence count drift")
        require(rendered.count(operation["text"]) == operation["count"],
                "Rendered removal occurrence count drift")
        original = original.replace(operation["text"], operation["replacement"])
        rendered = rendered.replace(operation["text"], operation["replacement"])
    require(digest(rendered) == APPROVED["after_rendered_sha256"],
            "Unreviewed rendered transformation")
    return {"content": original}


def validate_intake(request, ledger_bytes):
    require(set(request) == {"version", "stage", "target_id", "expected_content_sha256",
                             "payload_sha256", "payload", "dry_run_receipt_sha256"},
            "Unexpected intake fields")
    require(request["version"] == 1, "Unexpected intake version")
    require(request["stage"] in ("preflight", "dry-run", "apply"), "Unexpected stage")
    require(request["target_id"] == APPROVED["before"]["id"], "Unexpected post ID")
    require(request["expected_content_sha256"] ==
            APPROVED["before"]["rendered_sha256"]["content"], "Unexpected before digest")
    require(set(request["payload"]) == set(FIELDS), "Only the fixed content field is allowed")
    require(all(isinstance(v, str) and v for v in request["payload"].values()),
            "Empty or invalid candidate field")
    require(payload_digest(request["payload"]) == APPROVED["payload_sha256"] ==
            request["payload_sha256"], "Unreviewed candidate payload")
    require(hashlib.sha256(ledger_bytes).hexdigest() == APPROVED["ledger_sha256"],
            "Fact ledger changed")
    if request["stage"] != "apply":
        require(request["dry_run_receipt_sha256"] is None, "Unexpected apply approval")


def validate_identity(post):
    require(isinstance(post, dict), "Post lookup returned invalid data")
    for key, expected in APPROVED["before"].items():
        if key in ("rendered_sha256", "modified_gmt"):
            continue
        require(post.get(key) == expected, "Post identity/publication field drift: " + key)
    require(not post.get("password"), "Protected post outside this correction scope")
    for key in ("title", "content", "excerpt"):
        require(isinstance(post.get(key), dict) and
                isinstance(post[key].get("raw"), str) and
                isinstance(post[key].get("rendered"), str),
                "Edit-context field unavailable: " + key)


def validate_before(post):
    validate_identity(post)
    require(post.get("modified_gmt") == APPROVED["before"]["modified_gmt"],
            "Post modification time drift")
    require(rendered_hashes(post) == APPROVED["before"]["rendered_sha256"],
            "Current rendered content/title/excerpt digest drift")


def validate_dry_receipt(raw, request, implementation_sha):
    require(isinstance(raw, bytes) and
            hashlib.sha256(raw).hexdigest() == request["dry_run_receipt_sha256"],
            "Missing or changed reviewed dry-run receipt")
    receipt = json.loads(raw)
    require(receipt.get("state") == "dry_run_verified", "Dry-run did not verify")
    require(receipt.get("repository") == APPROVED["repository"] and
            receipt.get("post_id") == APPROVED["before"]["id"] and
            receipt.get("host") == APPROVED["host"], "Dry-run target mismatch")
    require(receipt.get("payload_sha256") == APPROVED["payload_sha256"] and
            receipt.get("ledger_sha256") == APPROVED["ledger_sha256"] and
            receipt.get("implementation_sha256") == implementation_sha,
            "Dry-run implementation or payload drift")
    require(receipt.get("before_rendered_sha256") == APPROVED["before"]["rendered_sha256"],
            "Dry-run before snapshot mismatch")
    require(str(receipt.get("run_id", "")).isdigit() and receipt.get("commit"),
            "Dry-run Action provenance absent")
    return receipt


class Runner:
    def __init__(self, client, request, ledger_bytes, save, context,
                 dry_receipt_bytes=None, implementation_sha=""):
        validate_intake(request, ledger_bytes)
        self.client = client
        self.request = request
        self.payload = request["payload"]
        self.save_callback = save
        self.receipt = {
            "state": "started", "repository": APPROVED["repository"],
            "host": APPROVED["host"], "post_id": APPROVED["before"]["id"],
            "stage": request["stage"], "payload_sha256": APPROVED["payload_sha256"],
            "ledger_sha256": APPROVED["ledger_sha256"],
            "implementation_sha256": implementation_sha, "run_id": context["run_id"],
            "commit": context["commit"], "writes_attempted": 0,
        }
        self.dry = None
        if request["stage"] == "apply":
            self.dry = validate_dry_receipt(dry_receipt_bytes, request, implementation_sha)

    def save(self, state, **fields):
        self.receipt.update(state=state, **fields)
        self.save_callback(copy.deepcopy(self.receipt))

    def after(self, post, baseline):
        validate_identity(post)
        require(identity(post) == baseline, "Unrelated fields changed after update")
        require(desired(post, self.payload), "Exact approved rendered change not confirmed")
        require(post["content"]["raw"] == self.payload["content"], "Unrelated raw content changed")
        require(all(raw_hashes(post)[k] == self.receipt["before_raw_sha256"][k]
                    for k in ("title", "excerpt")), "Untouched raw title or excerpt changed")
        self.receipt.update(after_raw_sha256=raw_hashes(post),
                            after_rendered_sha256=rendered_hashes(post),
                            after_modified_gmt=post.get("modified_gmt"),
                            preserved_identity=baseline)

    def run(self):
        self.save("prechecking")
        before = self.client.get_post()
        validate_identity(before)
        if desired(before, self.payload):
            self.save("already_applied_verified", after_raw_sha256=raw_hashes(before),
                      preserved_identity=identity(before))
            return self.receipt
        validate_before(before)
        self.payload = derive_payload(before)
        self.receipt["raw_payload_sha256"] = payload_digest(self.payload)
        baseline = identity(before)
        self.receipt.update(before_raw_sha256=raw_hashes(before),
                            before_rendered_sha256=rendered_hashes(before),
                            before_modified_gmt=before["modified_gmt"],
                            before_identity=baseline)
        self.save("preflight_verified")
        if self.request["stage"] == "preflight":
            return self.receipt
        if self.request["stage"] == "dry-run":
            self.save("dry_run_verified")
            return self.receipt
        require(self.dry["raw_payload_sha256"] == payload_digest(self.payload),
                "Candidate raw payload drift since dry-run")
        require(self.dry["before_raw_sha256"] == raw_hashes(before) and
                self.dry["before_identity"] == baseline, "Raw snapshot drift since dry-run")
        # A final authenticated same-identity read occurs immediately before the only update.
        fresh = self.client.get_post()
        validate_before(fresh)
        require(identity(fresh) == baseline and raw_hashes(fresh) == raw_hashes(before),
                "Fresh snapshot drift before write")
        self.client.approved_before_raw = raw_hashes(fresh)
        self.save("write_started", writes_attempted=1)
        try:
            response = self.client.update_post(self.payload)
            require(response.get("id") == APPROVED["before"]["id"],
                    "Update response identity mismatch")
        except HTTPFailureError as error:
            if error.status in (401, 403):
                self.save("denied_no_retry", http_status=error.status)
                raise
            return self.reconcile(baseline, before)
        except Exception:
            return self.reconcile(baseline, before)
        after = self.client.get_post()
        self.after(after, baseline)
        self.save("applied_verified")
        return self.receipt

    def reconcile(self, baseline, before):
        self.save("reconciling_same_post", writes_attempted=1)
        after = self.client.get_post()
        validate_identity(after)
        if desired(after, self.payload) and after["content"]["raw"] == self.payload["content"]:
            self.after(after, baseline)
            self.save("applied_reconciled_verified")
            return self.receipt
        if identity(after) == baseline and raw_hashes(after) == raw_hashes(before):
            self.save("not_applied_verified", automatic_retry=False)
            raise GuardError("Write not applied; no automatic retry")
        self.save("uncertain_stopped_for_review", automatic_retry=False)
        raise GuardError("Same-post outcome requires review; no retry")


class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


class ManagedClient:
    def __init__(self):
        require(os.environ.get("GITHUB_ACTIONS") == "true", "Managed Actions execution required")
        require(os.environ.get("GITHUB_REPOSITORY") == APPROVED["repository"],
                "Unexpected repository")
        require(os.environ.get("GITHUB_REF") == "refs/heads/" + APPROVED["branch"],
                "Unexpected correction branch")
        base = os.environ["WORDPRESS_BASE_URL"].rstrip("/")
        parsed = urllib.parse.urlsplit(base)
        require(parsed.scheme == "https" and parsed.hostname == APPROVED["host"] and
                parsed.path in ("", "/") and not parsed.query and not parsed.fragment and
                not parsed.username and not parsed.password, "Unexpected managed host")
        self.url = base + "/wp-json/wp/v2/posts/" + str(APPROVED["before"]["id"])
        user = os.environ["WORDPRESS_USERNAME"]
        password = os.environ["WORDPRESS_APP_PASSWORD"]
        self.authorization = "Basic " + base64.b64encode((user + ":" + password).encode()).decode()
        self.opener = urllib.request.build_opener(NoRedirect())

    def call(self, method, payload=None):
        # Endpoint is fixed in the constructor; no callers can pass an arbitrary URL or ID.
        url = self.url + ("?context=edit" if method == "GET" else "")
        data = None if payload is None else json.dumps(payload).encode("utf-8")
        headers = {"Authorization": self.authorization, "Accept": "application/json",
                   "Cache-Control": "no-cache", "User-Agent": "MNG-fixed-post-correction"}
        if data is not None:
            headers["Content-Type"] = "application/json"
        request = urllib.request.Request(url, data=data, headers=headers, method=method)
        try:
            with self.opener.open(request, timeout=45) as response:
                require(response.status == 200, "Unexpected successful HTTP status")
                value = json.loads(response.read())
                require(isinstance(value, dict), "Invalid post JSON")
                return value
        except urllib.error.HTTPError as error:
            raise HTTPFailureError(error.code) from None

    def get_post(self):
        return self.call("GET")

    def update_post(self, payload):
        require(set(payload) == {"content"}, "Only the fixed content operation is allowed")
        current = self.get_post()
        validate_before(current)
        require(getattr(self, "approved_before_raw", None) == raw_hashes(current),
                "Immediate raw snapshot drift before update")
        require(payload == derive_payload(current), "Update is not the exact fixed deletion")
        return self.call("POST", payload)


def main():
    receipt = {"state": "startup_failed", "writes_attempted": 0}
    OUTPUT.parent.mkdir(parents=True, exist_ok=True)

    def save(value):
        nonlocal receipt
        receipt = value
        temp = OUTPUT.with_suffix(".tmp")
        temp.write_text(json.dumps(value, ensure_ascii=False, indent=2))
        temp.replace(OUTPUT)

    try:
        request = json.loads(REQUEST.read_text())
        ledger = LEDGER.read_bytes()
        validate_intake(request, ledger)
        runner = Runner(
            ManagedClient(), request, ledger, save,
            {"run_id": os.environ["GITHUB_RUN_ID"], "commit": os.environ["GITHUB_SHA"]},
            DRY_RECEIPT.read_bytes() if request["stage"] == "apply" else None,
            hashlib.sha256(Path(__file__).read_bytes()).hexdigest(),
        )
        result = runner.run()
        print("FIXED_CORRECTION_RECEIPT " + json.dumps(result, sort_keys=True))
    except Exception as error:
        receipt["error_type"] = type(error).__name__
        if isinstance(error, GuardError):
            receipt["reason"] = str(error)
        if isinstance(error, HTTPFailureError):
            receipt["http_status"] = error.status
        if receipt.get("state") not in ("denied_no_retry", "not_applied_verified",
                                       "uncertain_stopped_for_review"):
            receipt["state"] = ("stopped_after_write" if receipt.get("writes_attempted")
                                else "stopped_before_write")
        save(receipt)
        print("FIXED_CORRECTION_STOPPED " + json.dumps(receipt, sort_keys=True))
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
