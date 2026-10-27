import random
import string
import time

from nova_launcher.core.desktop_entry import AppRecord
from nova_launcher.core.search_engine import SearchEngine, damerau_levenshtein, fuzzy_subsequence


def rec(name, id_=None, exe="", generic="", desc="", kw=(), cats=()):
    return AppRecord(id=id_ or name.lower().replace(" ", "-") + ".desktop", path="", name=name,
                     executable=exe, generic_name=generic, description=desc, keywords=kw, categories=cats)


APPS = [
    rec("Firefox", "firefox_firefox.desktop", "firefox", "Web Browser", cats=("Network", "WebBrowser")),
    rec("Files", "org.gnome.Nautilus.desktop", "nautilus", "File Manager", kw=("folder", "explorer"),
        cats=("Utility", "FileManager")),
    rec("File Roller", "org.gnome.FileRoller.desktop", "file-roller", "Archive Manager"),
    rec("Terminal", "org.gnome.Terminal.desktop", "gnome-terminal", cats=("System", "TerminalEmulator")),
    rec("Xfce Terminal", "xfce4-terminal.desktop", "xfce4-terminal", cats=("TerminalEmulator",)),
    rec("Visual Studio Code", "code_code.desktop", "code", "Text Editor", cats=("Development", "IDE")),
    rec("Settings", "org.gnome.Settings.desktop", "gnome-control-center"),
    rec("Calculator", "org.gnome.Calculator.desktop", "gnome-calculator", desc="Perform arithmetic"),
    rec("LibreOffice Calc", "libreoffice-calc.desktop", "libreoffice", "Spreadsheet"),
    rec("Google Chrome", "google-chrome.desktop", "google-chrome-stable", "Web Browser"),
]


def names(results):
    return [r.name for _, r in results]


def test_prompt_examples():
    e = SearchEngine(APPS)
    assert names(e.search("fire"))[0] == "Firefox"
    assert set(names(e.search("term"))[:2]) == {"Terminal", "Xfce Terminal"}
    assert names(e.search("code"))[0] == "Visual Studio Code"
    assert names(e.search("files"))[0] == "Files"


def test_exact_beats_prefix_beats_substring():
    e = SearchEngine([rec("Calc Tool"), rec("Calc"), rec("Supercalc")])
    assert names(e.search("calc")) == ["Calc", "Calc Tool", "Supercalc"]


def test_executable_keyword_and_category_matches():
    e = SearchEngine(APPS)
    assert names(e.search("nautilus"))[0] == "Files"  # executable
    assert "Files" in names(e.search("explorer"))  # keyword
    assert "Firefox" in names(e.search("browser"))  # generic name / category


def test_description_search_can_be_disabled():
    assert "Calculator" in names(SearchEngine(APPS).search("arithmetic"))
    assert "Calculator" not in names(SearchEngine(APPS, descriptions=False).search("arithmetic"))


def test_typos_and_fuzzy():
    e = SearchEngine(APPS)
    assert names(e.search("firefx"))[0] == "Firefox"
    assert names(e.search("setings"))[0] == "Settings"
    assert names(e.search("trminal"))[0] in ("Terminal", "Xfce Terminal")
    assert names(e.search("vsc"))[0] == "Visual Studio Code"  # initials
    assert SearchEngine(APPS, fuzzy=False).search("firefx") == []


def test_multi_word_queries_require_every_term():
    e = SearchEngine(APPS)
    assert names(e.search("libre calc")) == ["LibreOffice Calc"]
    assert e.search("firefox zzzz") == []


def test_accents_and_case_are_ignored():
    e = SearchEngine([rec("Écran Tool")])
    assert names(e.search("ECRAN")) == ["Écran Tool"]


def test_empty_and_symbol_queries_return_nothing():
    e = SearchEngine(APPS)
    assert e.search("") == [] and e.search("   ") == [] and e.search("$(rm -rf ~)") == []


def test_usage_boost_breaks_ties_only():
    pair = [rec("Alpha Terminal", "a.desktop"), rec("Beta Terminal", "b.desktop")]
    usage = {"b.desktop": 10.0}
    e = SearchEngine(pair + APPS, usage_score=lambda i: usage.get(i, 0.0))
    assert names(e.search("terminal"))[0] == "Terminal"  # exact match still wins
    assert names(e.search("terminal"))[1] == "Beta Terminal"  # same tier, used more
    assert names(e.search("fire"))[0] == "Firefox"  # boost cannot jump tiers


def test_helpers():
    assert damerau_levenshtein("firefox", "firefx", 2) == 1
    assert damerau_levenshtein("ab", "ba", 1) == 1
    assert damerau_levenshtein("abc", "xyzuvw", 1) == 2
    assert fuzzy_subsequence("ff", "firefox") > 0
    assert fuzzy_subsequence("zz", "firefox") == 0


def test_latency_with_1000_apps():
    rng = random.Random(1)
    words = ["".join(rng.choices(string.ascii_lowercase, k=rng.randint(4, 9))) for _ in range(400)]
    records = APPS + [rec(" ".join(rng.sample(words, 2)).title(), f"app{i}.desktop", f"bin{i}",
                          desc=" ".join(rng.sample(words, 6))) for i in range(990)]
    e = SearchEngine(records)
    timings = []
    for q in ["f", "fi", "fir", "fire", "firefx", "term", "code", "setings", "xyzq", "libre calc"]:
        t = time.perf_counter()
        e.search(q, 20)
        timings.append((time.perf_counter() - t) * 1000)
    timings.sort()
    assert timings[-1] < 50, f"worst search {timings[-1]:.1f} ms (target < 50 ms)"
