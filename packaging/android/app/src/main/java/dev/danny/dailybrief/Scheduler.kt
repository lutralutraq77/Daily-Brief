package dev.danny.dailybrief

import android.content.Context
import androidx.work.BackoffPolicy
import androidx.work.Constraints
import androidx.work.ExistingPeriodicWorkPolicy
import androidx.work.ExistingWorkPolicy
import androidx.work.NetworkType
import androidx.work.OneTimeWorkRequestBuilder
import androidx.work.PeriodicWorkRequestBuilder
import androidx.work.WorkManager
import java.util.Calendar
import java.util.concurrent.TimeUnit

object Scheduler {

    private const val DAILY = "dailybrief-daily"
    private const val NOW = "dailybrief-now"

    private val network = Constraints.Builder()
        .setRequiredNetworkType(NetworkType.CONNECTED)
        .build()

    /**
     * Runs once a day, first firing at the next [hour]:00.
     *
     * WorkManager is not an alarm clock -- it batches work and will drift by
     * minutes. That is the right trade here: a brief that arrives at 08:04
     * instead of 08:00 is fine, and an exact alarm would cost battery for
     * nothing.
     */
    fun scheduleDaily(context: Context, hour: Int = 8) {
        val request = PeriodicWorkRequestBuilder<BriefWorker>(1, TimeUnit.DAYS)
            .setConstraints(network)
            .setInitialDelay(millisUntil(hour), TimeUnit.MILLISECONDS)
            .setBackoffCriteria(BackoffPolicy.EXPONENTIAL, 15, TimeUnit.MINUTES)
            .build()
        WorkManager.getInstance(context.applicationContext)
            // KEEP, not UPDATE: this runs on every launch, and UPDATE re-applies
            // initialDelay against the ORIGINAL lastEnqueueTime, so opening the
            // app before the first run drags the daily slot to that moment.
            // KEEP still creates the work on first launch and after a cancel.
            .enqueueUniquePeriodicWork(DAILY, ExistingPeriodicWorkPolicy.KEEP, request)
    }

    fun runNow(context: Context) {
        val request = OneTimeWorkRequestBuilder<BriefWorker>()
            .setConstraints(network)
            .build()
        WorkManager.getInstance(context.applicationContext)
            .enqueueUniqueWork(NOW, ExistingWorkPolicy.REPLACE, request)
    }

    private fun millisUntil(hour: Int): Long {
        val now = Calendar.getInstance()
        val target = Calendar.getInstance().apply {
            set(Calendar.HOUR_OF_DAY, hour)
            set(Calendar.MINUTE, 0)
            set(Calendar.SECOND, 0)
            set(Calendar.MILLISECOND, 0)
            if (!after(now)) add(Calendar.DAY_OF_MONTH, 1)
        }
        return target.timeInMillis - now.timeInMillis
    }
}
