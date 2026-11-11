#!/usr/bin/env bash
# The run behind examples/mobile_audit.example.json. Run from the repository root with one Android
# emulator running (here: AVD "qa_skills_pixel_api36", system image android-36 google_apis x86_64, Pixel 7).
# The APK was built from examples/harbour-dental-app with `gradle assembleRelease` (see its README).
set -euo pipefail
S=mobile-app-auditor-skill
R=$S/examples/run
PKG=au.example.harbourdental

python $S/scripts/check_tools.py --out $R/tools.json
adb install -r $R/app-release.apk
adb shell pm clear $PKG

# 1. Guided exploration: screen graph, sampled screens, crashes and ANRs
python $S/scripts/explore.py --package $PKG --activity .MainActivity --out $R --max-actions 30

# 2. Checks and probes on every sampled screen (every changed setting is restored)
python $S/scripts/run_mobile_audit.py --run $R --apk $R/app-release.apk

# 3. Replay each crash twice on a fresh app state (monkey stays off: exploration "guided")
python $S/scripts/crash_explore.py --run $R --replay --attempts 2

# 4. Candidates
python $S/scripts/merge_findings.py --run $R --out $R/candidates.json

# 5. Validate the audit against the run
python $S/scripts/validate_mobile_audit.py $S/examples/mobile_audit.example.json --run $R
