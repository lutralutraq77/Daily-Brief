package dev.danny.dailybrief

import android.app.Application
import com.chaquo.python.Python
import com.chaquo.python.android.AndroidPlatform

class DailyBriefApp : Application() {

    override fun onCreate() {
        super.onCreate()
        // Chaquopy's interpreter is process-wide and must be started before any
        // Python is touched, including from a background worker.
        if (!Python.isStarted()) {
            Python.start(AndroidPlatform(this))
        }
        Notifications.createChannel(this)
    }
}
