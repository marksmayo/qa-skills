# harbour-dental-app

A small Android app (Kotlin, Jetpack Compose) for the fictional Harbour Dental clinic, the same clinic as the
WCAG auditor's sample site. It has planted accessibility barriers, a crash and an ANR, for the mobile app
auditor's example; the answer key is in `../README.md`. Everything is invented, and the app has no backend.

    gradle assembleRelease      # Gradle 9.6, JDK 17, Android SDK 36; shrunk, not obfuscated, signed with the debug key
                            # output: app/build/outputs/apk/release/app-release.apk

The built APK used by the example run is `../run/app-release.apk`, with its SHA-256 in `../run/run.json`.
