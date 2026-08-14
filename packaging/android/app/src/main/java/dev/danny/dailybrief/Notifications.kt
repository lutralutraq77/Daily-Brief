package dev.danny.dailybrief

import android.Manifest
import android.app.NotificationChannel
import android.app.NotificationManager
import android.app.PendingIntent
import android.content.Context
import android.content.Intent
import android.content.pm.PackageManager
import android.os.Build
import androidx.core.app.NotificationCompat
import androidx.core.app.NotificationManagerCompat
import androidx.core.content.ContextCompat

object Notifications {

    const val CHANNEL = "daily_brief"
    private const val NOTIFICATION_ID = 1

    fun createChannel(context: Context) {
        // API 26+ only. minSdk is 24, and pre-O has no channels at all --
        // NotificationManagerCompat ignores the channel id there, so postResult
        // is unaffected. Touching the class on 24/25 is NoClassDefFoundError in
        // Application.onCreate, i.e. the app never launches.
        if (Build.VERSION.SDK_INT < Build.VERSION_CODES.O) return
        val manager = context.getSystemService(NotificationManager::class.java) ?: return
        manager.createNotificationChannel(
            NotificationChannel(
                CHANNEL,
                context.getString(R.string.brief_channel_name),
                NotificationManager.IMPORTANCE_DEFAULT,
            ).apply {
                description = context.getString(R.string.brief_channel_desc)
                setShowBadge(true)
            },
        )
    }

    /** Says what actually happened; a failed run is not announced as a ready brief. */
    fun postResult(context: Context, result: RunResult) {
        // POST_NOTIFICATIONS does not exist before API 33, so checkSelfPermission
        // answers DENIED for it there and every 24-32 device would silently get
        // no notification at all. Below 33 the permission is not required.
        if (Build.VERSION.SDK_INT >= Build.VERSION_CODES.TIRAMISU) {
            val granted = ContextCompat.checkSelfPermission(
                context,
                Manifest.permission.POST_NOTIFICATIONS,
            ) == PackageManager.PERMISSION_GRANTED
            if (!granted) return
        }

        val open = PendingIntent.getActivity(
            context,
            0,
            Intent(context, MainActivity::class.java).addFlags(Intent.FLAG_ACTIVITY_CLEAR_TOP),
            PendingIntent.FLAG_UPDATE_CURRENT or PendingIntent.FLAG_IMMUTABLE,
        )

        val title = if (result.ok) "Your brief is ready" else "The brief could not be generated"
        val body = when {
            !result.ok -> result.error?.lines()?.lastOrNull { it.isNotBlank() } ?: "Unknown error"
            result.sections.isNotBlank() -> result.sections
            else -> "Tap to read it"
        }

        val notification = NotificationCompat.Builder(context, CHANNEL)
            .setSmallIcon(android.R.drawable.ic_dialog_info)
            .setContentTitle(title)
            .setContentText(body)
            .setStyle(NotificationCompat.BigTextStyle().bigText(body))
            .setAutoCancel(true)
            .setContentIntent(open)
            .build()

        NotificationManagerCompat.from(context).notify(NOTIFICATION_ID, notification)
    }
}
