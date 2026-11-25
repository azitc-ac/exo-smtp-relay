"""Stable-Releases brauchen die Freigabe eines Menschen (Environment „release“).

Veröffentlicht (Image-Push, GitHub-Release) wird erst nach dem Job „freigabe“;
für den Kanal stable läuft der im Environment „release“ mit Pflicht-Prüfer.
"""
from pathlib import Path

import re

WF = Path(__file__).resolve().parents[1] / ".github" / "workflows" / "release.yml"


def _jobs():
    """Jobs mit ihren Feldern needs/environment – ohne YAML-Bibliothek
    (die steht nicht in der Lock-Datei)."""
    text = WF.read_text(encoding="utf-8").split("\njobs:\n", 1)[1]
    jobs = {}
    for teil in re.split(r"\n(?=  [A-Za-z_-]+:\n)", "\n" + text):
        m = re.match(r"\s*([A-Za-z_-]+):\n", teil)
        if not m:
            continue
        job = {}
        n = re.search(r"^    needs: *(.+)$", teil, re.M)
        if n:
            job["needs"] = [x.strip() for x in n.group(1).strip("[] ").split(",") if x.strip()]
        e = re.search(r"^    environment: *(.+)$", teil, re.M)
        if e:
            job["environment"] = e.group(1)
        jobs[m.group(1)] = job
    return jobs


def _needs(job):
    return job.get("needs", [])


def test_freigabe_job_nutzt_release_environment_fuer_stable():
    env = _jobs()["freigabe"]["environment"]
    assert "'stable'" in env and "'release'" in env


def test_veroeffentlichung_nur_nach_freigabe():
    jobs = _jobs()

    def haengt_ab(name, ziel, gesehen=None):
        gesehen = gesehen or set()
        for n in _needs(jobs[name]):
            if n == ziel or (n not in gesehen and haengt_ab(n, ziel, gesehen | {n})):
                return True
        return False

    for name in ("image", "manifest", "release"):
        assert haengt_ab(name, "freigabe"), f"{name} veröffentlicht ohne Freigabe"
