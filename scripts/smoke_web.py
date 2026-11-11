"""Exercise a disposable code-review server, including persistence after restart.

This creates one evaluation; use only a development/CI server, never production.
"""
import argparse
import json
from urllib.request import Request, urlopen


def request(base_url, path, body=None):
    data = None if body is None else json.dumps(body).encode()
    headers = {"Content-Type": "application/json", "X-Requested-With": "PaperLingo"}
    with urlopen(Request(base_url + path, data=data, headers=headers), timeout=20) as response:
        return json.load(response)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--url", default="http://127.0.0.1:4173")
    parser.add_argument("--run-id", help="Check an existing record instead of creating one")
    args = parser.parse_args()
    base = args.url.rstrip("/")
    assert request(base, "/api/health")["status"] == "ok"
    if args.run_id:
        run = request(base, f"/api/runs/{args.run_id}")
    else:
        sample = request(base, "/api/sample")["predictions"]
        run = request(base, "/api/runs", {"name": "Container smoke test", "predictions": sample})
    assert run["status"] == "complete", run["status"]
    assert run["report"]["review_mode"] == "code"
    assert run["report"]["metrics"]["final_score"] == 100
    print(run["id"])


if __name__ == "__main__":
    main()
