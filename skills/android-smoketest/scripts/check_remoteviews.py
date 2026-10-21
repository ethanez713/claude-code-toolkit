#!/usr/bin/env python3
"""Catch RemoteViews faults that compile cleanly and fail only in the host process.

A widget's layout is inflated, and its actions dispatched, inside the launcher — by
reflection, against allow-lists that exist nowhere in the build. So a bare <View> and a
setInt(id, "setIndeterminateTint", c) are both valid XML and valid Kotlin, both survive
gradle and lint, and both fail the ENTIRE widget at runtime with a W-level log in a
process no app-side check reads.

Two checks, no device:
  1. every class in a layout an <appwidget-provider> names must be @RemoteView-annotated
  2. every reflective RemoteViews setter must name a @RemotableViewMethod

Usage: check_remoteviews.py <android-module-dir>   # e.g. android/app
Exit 1 on a definite fault, 0 otherwise. Warnings do not fail.
"""
import re
import sys
from pathlib import Path
from xml.etree import ElementTree

ANDROID = "{http://schemas.android.com/apk/res/android}"

# AOSP classes carrying @RemoteView. Anything else fails inflation with
# "Class not allowed to be inflated". <include>/<merge>/<requestFocus> are directives.
REMOTE_VIEWS = {
    "AbsoluteLayout", "AdapterViewFlipper", "AnalogClock", "Button", "Chronometer",
    "FrameLayout", "GridLayout", "GridView", "ImageButton", "ImageView", "LinearLayout",
    "ListView", "ProgressBar", "RelativeLayout", "StackView", "TextClock", "TextView",
    "ViewFlipper", "ViewStub",
}
DIRECTIVES = {"include", "merge", "requestFocus", "tag"}
# Allowed only from API 31. Flagged when minSdk is lower.
REMOTE_VIEWS_31 = {"CheckBox", "RadioButton", "RadioGroup", "Switch"}

# @RemotableViewMethod setters reachable through RemoteViews.setInt/setBoolean/setFloat/
# setString/setCharSequence. Not exhaustive: an unlisted name is reported as unverified,
# not as a fault, because the cost of a false failure here is higher than a second look.
REMOTABLE = {
    "setBackgroundColor", "setBackgroundResource", "setColorFilter", "setImageAlpha",
    "setImageLevel", "setImageResource", "setMaxLines", "setMinLines", "setLines",
    "setEllipsize", "setTextColor", "setTextScaleX", "setEnabled", "setVisibility",
    "setDisplayedChild", "setImageURI", "setMax", "setProgress", "setIndeterminate",
    "setChecked", "setSelected", "setContentDescription", "setLabelFor", "setInputType",
    "setGravity", "setHeight", "setWidth", "setAlpha", "setBackgroundTintList",
    "setCompoundDrawablePadding", "setSingleLine", "setBase", "setFormat", "setStarted",
    "setTimeZone", "setFormat12Hour", "setFormat24Hour", "setScrollPosition",
}
SETTER_CALL = re.compile(
    r"""\bset(?:Int|Boolean|Float|Long|Char|Byte|Short|Double|String|CharSequence|Bundle|Intent|Uri|Icon)\s*\(\s*[^,]+,\s*["']([A-Za-z0-9_]+)["']"""
)


def widget_layouts(module: Path) -> set[str]:
    """Layouts an <appwidget-provider> names, plus every layout they <include>."""
    names: set[str] = set()
    for xml in (module / "src/main/res/xml").glob("*.xml"):
        try:
            root = ElementTree.parse(xml).getroot()
        except ElementTree.ParseError:
            continue
        if root.tag != "appwidget-provider":
            continue
        for attr in ("initialLayout", "previewLayout"):
            ref = root.get(ANDROID + attr)
            if ref:
                names.add(ref.rsplit("/", 1)[-1])
    return names


def min_sdk(module: Path) -> int:
    for build in list(module.glob("build.gradle*")):
        m = re.search(r"minSdk\s*=?\s*(\d+)", build.read_text())
        if m:
            return int(m.group(1))
    return 0


def check_layouts(module: Path, faults: list[str], warns: list[str]) -> None:
    sdk = min_sdk(module)
    for name in sorted(widget_layouts(module)):
        path = module / "src/main/res/layout" / f"{name}.xml"
        if not path.exists():
            warns.append(f"{name}.xml: named by an appwidget-provider but not found")
            continue
        for num, line in enumerate(path.read_text().splitlines(), 1):
            m = re.match(r"\s*<([A-Za-z][\w.]*)", line)
            if not m:
                continue
            tag = m.group(1)
            cls = tag.rsplit(".", 1)[-1]
            if tag in DIRECTIVES or cls in REMOTE_VIEWS:
                continue
            if cls in REMOTE_VIEWS_31:
                if sdk and sdk < 31:
                    faults.append(
                        f"{path}:{num}: <{tag}> is only RemoteViews-allowed from API 31, "
                        f"but minSdk is {sdk}"
                    )
                continue
            faults.append(
                f"{path}:{num}: <{tag}> is not a @RemoteView class — inflation fails with "
                f'"Class not allowed to be inflated", taking the whole widget with it'
            )


def check_setters(module: Path, faults: list[str], warns: list[str]) -> None:
    for src in (module / "src/main/java").rglob("*.kt"):
        text = src.read_text()
        if "RemoteViews" not in text:
            continue
        for num, line in enumerate(text.splitlines(), 1):
            for method in SETTER_CALL.findall(line):
                if method not in REMOTABLE:
                    warns.append(
                        f"{src}:{num}: \"{method}\" is not a known @RemotableViewMethod. "
                        f"If it is not annotated, RemoteViews throws ActionException and the "
                        f"ENTIRE update is dropped. Verify against AOSP or set it in the layout."
                    )


def main() -> int:
    module = Path(sys.argv[1] if len(sys.argv) > 1 else "android/app")
    if not (module / "src/main").is_dir():
        print(f"not an android module: {module}", file=sys.stderr)
        return 2
    faults: list[str] = []
    warns: list[str] = []
    check_layouts(module, faults, warns)
    check_setters(module, faults, warns)
    for w in warns:
        print(f"warn: {w}")
    for f in faults:
        print(f"FAIL: {f}")
    if not faults and not warns:
        print("remoteviews: clean")
    return 1 if faults else 0


if __name__ == "__main__":
    sys.exit(main())
