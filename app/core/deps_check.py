import importlib.util

_REQUIRED_EXTRACTOR_DEPS = {
    "google.genai":            "google-genai (native-PDF vision rung)",
    "langchain_google_genai":  "langchain-google-genai (Gemini text)",
    "pandas":                  "pandas (.xls/.xlsx spec extraction)",
    "xlrd":                    "xlrd (legacy .xls engine)",
    "fitz":                    "PyMuPDF (PDF content layer)",
    "ezdxf":                   "ezdxf (DXF parsing)",
    "shapely":                 "shapely (DXF geometry)",
}

def verify_extractor_deps(*, strict: bool = True) -> list[str]:
    """Loud startup check. A missing extractor dep otherwise fails silently deep in
    the pipeline (vision rung returns None, .xls throws) — surface it at boot."""
    missing = [f"{mod} — {why}" for mod, why in _REQUIRED_EXTRACTOR_DEPS.items()
               if importlib.util.find_spec(mod) is None]
    if missing and strict:
        raise RuntimeError("Missing extractor dependencies:\n  " + "\n  ".join(missing))
    return missing