"""Translation completeness and the language-independent submission contract."""
import json
import re
from html.parser import HTMLParser

from fastapi.testclient import TestClient

from paperlingo.app import create_app
from paperlingo.config import ROOT, Settings

STATIC = ROOT / "paperlingo" / "static"


def catalog():
    source = (STATIC / "translations.js").read_text()
    return json.loads(source.split("window.PaperLingoEnglish = ", 1)[1].rstrip().removesuffix(";"))


def test_translation_placeholders_and_dynamic_keys_are_complete():
    entries = catalog()
    for source, english in entries.items():
        assert english.strip(), source
        assert not re.search("[가-힣]", english), source
        assert sorted(re.findall(r"\{\d+\}", source)) == sorted(re.findall(r"\{\d+\}", english)), source
    for literal in re.findall(r'msg\(("(?:[^"\\]|\\.)*")', (STATIC / "app.js").read_text()):
        key = json.loads(literal)
        if re.search("[가-힣]", key):
            assert key in entries, key


def test_static_ui_text_and_accessibility_labels_are_translated():
    entries = catalog()

    class VisibleStrings(HTMLParser):
        def __init__(self):
            super().__init__()
            self.skip = []

        def check(self, value):
            if value and re.search("[가-힣]", value):
                assert value.strip() in entries, value

        def handle_starttag(self, tag, attrs):
            attrs = dict(attrs)
            if tag in {"script", "style", "pre", "textarea"} or "data-i18n-skip" in attrs:
                self.skip.append(tag)
            if not self.skip:
                for name in ("placeholder", "title", "aria-label"):
                    self.check(attrs.get(name))

        def handle_endtag(self, tag):
            if self.skip and self.skip[-1] == tag:
                self.skip.pop()

        def handle_data(self, text):
            if not self.skip:
                self.check(text)

    VisibleStrings().feed((STATIC / "index.html").read_text())


def test_english_schema_translates_help_but_preserves_contract(tmp_path):
    with TestClient(create_app(Settings(runtime_dir=tmp_path))) as client:
        ko = client.get("/api/schema").json()
        en = client.get("/api/schema?lang=en").json()
        assert en["input"] == catalog()[ko["input"]]
        assert en["context_rule"] == catalog()[ko["context_rule"]]
        for name in ko["question_fields"]:
            assert en["question_fields"][name]["description"] == catalog()[ko["question_fields"][name]["description"]]
            en["question_fields"][name]["description"] = ko["question_fields"][name]["description"]
        en["input"], en["context_rule"] = ko["input"], ko["context_rule"]
        assert en == ko
        assert client.get("/api/schema?lang=invalid").status_code == 422
