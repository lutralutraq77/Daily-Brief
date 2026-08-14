# Minification is off for release (see build.gradle.kts). These rules exist so
# that turning it on later does not break Chaquopy, which reaches into the
# Python runtime by name.
-keep class com.chaquo.python.** { *; }
-dontwarn com.chaquo.python.**

-keep class * extends androidx.work.ListenableWorker {
    public <init>(android.content.Context, androidx.work.WorkerParameters);
}
