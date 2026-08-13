"""Cohort building specific to ultrasound-CT pairings.

The modules here assume a pairing of exactly two modalities, an ultrasound
followed by a CT: ``match`` names its inputs ``--us_csv`` / ``--ct_csv`` and
its output columns ``us_*`` / ``ct_*``, and ``cohort`` lays exams out under
``<mrn>/<date>/{us,ct}/``, keeping every series of the ultrasound and only
the thinnest axial series of the CT.

Anything that generalises past that pairing belongs outside this package --
``air_download.probe``, for instance, reads a pairing's modalities off the
CSV header and works for any of them.
"""
