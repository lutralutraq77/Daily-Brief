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
        Notifications.postResult(applicationContext, result)
        return when {
            result.ok -> Result.success()
            runAttemptCount < 3 -> Result.retry()
            else -> Result.failure()
        }
    }
}
