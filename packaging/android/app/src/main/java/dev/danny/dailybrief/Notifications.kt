package dev.danny.dailybrief

import android.Manifest
import android.app.NotificationChannel
import android.app.NotificationManager
import android.app.PendingIntent
import android.content.Context
import android.content.Intent
import android.content.pm.PackageManager
import android.os.Build
import android.net.Uri
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
        if (result.busy) return
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
        val readings = listOf("Paper" to result.previews.paper, "Climate" to result.previews.climate)
        val body = when {
            !result.ok -> result.error?.lines()?.lastOrNull { it.isNotBlank() } ?: "Unknown error"
            else -> readings.joinToString("\n") { (label, preview) ->
                "$label: ${preview.title.ifBlank { "No preview available" }}"
            }
        }
        val expanded = if (!result.ok) body else readings.joinToString("\n\n") { (label, preview) ->
            listOf("$label · ${preview.title.ifBlank { "No preview available" }}",
                listOf(preview.source, preview.published.take(10)).filter { it.isNotBlank() }.joinToString(" · "),
                preview.summary, preview.note).filter { it.isNotBlank() }.joinToString("\n")
        }

        val builder = NotificationCompat.Builder(context, CHANNEL)
            .setSmallIcon(R.drawable.ic_notification)
            .setContentTitle(title)
            .setContentText(body)
            .setStyle(NotificationCompat.BigTextStyle().bigText(expanded))
            .setSubText(result.previews.date.takeIf { result.ok && it.isNotBlank() })
            .setCategory(NotificationCompat.CATEGORY_RECOMMENDATION)
            .setAutoCancel(true)
            .setContentIntent(open)
        if (result.ok) {
            builder.addAction(R.drawable.ic_notification, "Open brief", open)
            readings.forEachIndexed { index, (label, preview) ->
                if (preview.url.startsWith("https://") || preview.url.startsWith("http://")) {
                    val article = PendingIntent.getActivity(context, index + 1,
                        Intent(Intent.ACTION_VIEW, Uri.parse(preview.url)),
                        PendingIntent.FLAG_UPDATE_CURRENT or PendingIntent.FLAG_IMMUTABLE)
                    builder.addAction(R.drawable.ic_notification, "Read ${label.lowercase()}", article)
                }
            }
        }

        NotificationManagerCompat.from(context).notify(NOTIFICATION_ID, builder.build())
    }
}
