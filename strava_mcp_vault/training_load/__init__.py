"""Training-load calculations on top of the Strava vault.

Phase 1: athlete configuration layer (FTP, LTHR, weight) with effective-date
history. Future phases will add per-activity TSS/NP/IF computation and
time-series CTL/ATL/TSB.

All training-load tools follow Coggan / TrainingPeaks methodology and
return both their computed values and the inputs used — so when numbers
look wrong, the user can see why without reading code.
"""
