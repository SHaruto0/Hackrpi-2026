"""Send floor plan images to Gemini, validate, and let Gemini fix what the validator finds.

Usage (from project root):
    export GEMINI_API_KEY=...
    python -m backend.mapping.extract_floorplans f1.png f2.png f3.png -o data/buildings/dcc-gemini.json
    # refine an existing map instead of extracting from scratch:
    python -m backend.mapping.extract_floorplans f1.png f2.png f3.png --start data/buildings/dcc-gemini.json -o data/buildings/dcc-gemini.json
Then:
    FIRESIGHT_BUILDING=data/buildings/dcc-gemini.json uvicorn backend.server:app --port 8000

Images must be passed in floor order (first = floor 1). Each round's output is saved as
<out>.r<k>.json. Rounds stop early when the validator (backend/checks.py) finds nothing.
The round with the fewest issues is written to <out>. Always review it by hand after.
"""
import argparse
import json
import mimetypes
import os
import sys
import time

from ..core.checks import check_building
from ..core.model import ModelError, load_building, parse_json_text

HERE = os.path.dirname(os.path.abspath(__file__))
PROMPT_PATH = os.path.join(HERE, "prompts", "gemini_prompt.txt")
FIX_PROMPT_PATH = os.path.join(HERE, "prompts", "gemini_fix_prompt.txt")
MAX_ISSUES_SENT = 80


def _read(path):
    with open(path, encoding="utf-8") as fh:
        return fh.read()


def evaluate(raw):
    """Return (issues, error). error is set when the JSON can't be loaded at all."""
    try:
        return check_building(load_building(raw)), None
    except (ModelError, ValueError) as e:
        return None, str(e)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("images", nargs="*", help="floor plan images/PDFs, in floor order")
    ap.add_argument("-o", "--out", default="data/buildings/building.json")
    ap.add_argument("--model", default=os.environ.get("GEMINI_MODEL", "gemini-3.1-pro-preview"))
    ap.add_argument("--building", default="Darrin Communications Center (RPI)")
    ap.add_argument("--first-level", type=int, default=1)
    ap.add_argument("--rounds", type=int, default=3, help="max fix rounds after the first answer")
    ap.add_argument("--start", help="existing building JSON to refine instead of extracting fresh")
    ap.add_argument("--api-version", default=os.environ.get("GEMINI_API_VERSION", "v1beta"),
                    help="Gemini API version (preview models need v1beta)")
    ap.add_argument("--list-models", action="store_true", help="print models your key can use and exit")
    args = ap.parse_args()

    from google import genai
    from google.genai import types

    # reads GEMINI_API_KEY
    client = genai.Client(http_options=types.HttpOptions(api_version=args.api_version))

    if args.list_models:
        for m in client.models.list():
            if "generateContent" in (getattr(m, "supported_actions", None) or []):
                print(m.name.removeprefix("models/"))
        return
    if not args.images:
        ap.error("pass the floor plan files in floor order")

    n = len(args.images)
    order = ", ".join(f"Image {i + 1} = Floor {args.first_level + i}" for i in range(n))
    image_parts = []
    for i, path in enumerate(args.images):
        mime = mimetypes.guess_type(path)[0] or "image/png"
        with open(path, "rb") as fh:
            data = fh.read()
        image_parts.append(f"Image {i + 1} (Floor {args.first_level + i}):")
        image_parts.append(types.Part.from_bytes(data=data, mime_type=mime))

    config = types.GenerateContentConfig(response_mime_type="application/json", temperature=0.1)

    def ask(text):
        """Return Gemini's text, or None if the API keeps failing."""
        for attempt in range(5):
            try:
                resp = client.models.generate_content(model=args.model, contents=[text] + image_parts,
                                                      config=config)
                return resp.text or ""
            except genai.errors.APIError as e:
                temporary = e.code in (500, 502, 503, 504) or (e.code == 429 and "PerDay" not in str(e))
                if not temporary or attempt == 4:
                    print(f"Gemini API error ({args.model}): {e}\n"
                          f"Tip: set GEMINI_MODEL or pass --model to try another model.", file=sys.stderr)
                    return None
                wait = 15 * 2 ** attempt
                print(f"  API busy ({e.code}), retrying in {wait}s ...")
                time.sleep(wait)

    stem = os.path.splitext(args.out)[0]
    os.makedirs(os.path.dirname(os.path.abspath(args.out)), exist_ok=True)

    if args.start:
        text = _read(args.start)
        print(f"Starting from {args.start}")
    else:
        prompt = (_read(PROMPT_PATH).replace("{BUILDING}", args.building)
                  .replace("{N}", str(n)).replace("{ORDER}", order))
        print(f"Round 0: extracting with {args.model} ...")
        text = ask(prompt)
        if text is None:
            sys.exit(1)

    best = None   # (issue_count, round, raw)
    for k in range(args.rounds + 1):
        round_path = f"{stem}.r{k}.json"
        with open(round_path, "w", encoding="utf-8") as fh:
            fh.write(text)
        try:
            raw = parse_json_text(text)
        except (ModelError, ValueError) as e:
            raw, issues, err = None, None, f"not valid JSON: {e}"
        else:
            issues, err = evaluate(raw)

        if err:
            print(f"Round {k}: failed to load ({err}). Saved {round_path}")
            feedback = [f"The JSON could not be loaded: {err}"]
        else:
            print(f"Round {k}: {len(issues)} issue(s). Saved {round_path}")
            if best is None or len(issues) < best[0]:
                best = (len(issues), k, raw)
            if not issues:
                break
            feedback = issues

        if k == args.rounds:
            break
        shown = feedback[:MAX_ISSUES_SENT]
        if len(feedback) > len(shown):
            shown.append(f"... and {len(feedback) - len(shown)} more of the same kinds")
        fix = (_read(FIX_PROMPT_PATH).replace("{ISSUES}", "\n".join(f"- {i}" for i in shown))
               .replace("{JSON}", text if raw is None else json.dumps(raw)))
        print(f"Round {k + 1}: asking {args.model} to fix ...")
        text = ask(fix)
        if text is None:
            print("Stopping early; keeping the best round so far.")
            break

    if best is None:
        print("No round produced a loadable model. Check the .r*.json files.", file=sys.stderr)
        sys.exit(1)

    count, k, raw = best
    with open(args.out, "w", encoding="utf-8") as fh:
        json.dump(raw, fh, indent=2)
    print(f"\nSaved {args.out} from round {k} with {count} remaining issue(s):")
    for i in check_building(load_building(raw)):
        print("-", i)


if __name__ == "__main__":
    main()
