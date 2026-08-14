package dev.danny.dailybrief

import android.content.Context
import androidx.work.CoroutineWorker
import androidx.work.WorkerParameters

class BriefWorker(
    context: Context,
    params: WorkerParameters,
) : CoroutineWorker(context, params) {

    override suspend fun doWork(): Result {
        val result = Brief.run(applicationContext)
        // Busy means the Refresh button is already inside cmd_run in this same
        // interpreter. Nothing failed, so post nothing -- announcing "The brief
        // could not be generated" for a run that is in progress is a lie. Come
        // back on the backoff instead.
        if (result.busy) return Result.retry()
        Notifications.postResult(applicationContext, result)
        return when {
            result.ok -> Result.success()
            runAttemptCount < 3 -> Result.retry()
            else -> Result.failure()
        }
    }
}
