package dev.danny.dailybrief

import android.content.Context
import com.chaquo.python.Python
import kotlinx.coroutines.Dispatchers
import kotlinx.coroutines.withContext
import org.json.JSONObject

/**
 * A connected calendar, as the UI is allowed to see it.
 *
 * [masked] is deliberately not the address. A Google secret iCal URL is a
 * permanent bearer token for the whole calendar, so Python hands back only
 * enough to tell two calendars apart -- the host and the last few characters.
 */
data class CalendarEntry(
    val label: String,
    val masked: String,
    val isUrl: Boolean,
)

/**
 * The three states of reading calendars.txt, kept apart.
 *
 * An empty [entries] with a null [error] means "no calendar is connected". A
 * non-null [error] means the file could not be read, which is a different thing
 * and must never render as the same thing.
 */
data class CalendarList(
    val entries: List<CalendarEntry> = emptyList(),
    val error: String? = null,
)

data class CalendarCheck(
    val ok: Boolean,
    val status: String,
    val reason: String,
    val events: Int,
)

object Calendars {

    private fun entry() = Python.getInstance().getModule("android_entry")

    private val ADDRESS = Regex("""\b(?:https?|webcal|file)://\S+""", RegexOption.IGNORE_CASE)

    /**
     * The reason a read failed is a Python traceback, and every line of
     * calendars.txt is a permanent bearer token. Python should never put one in
     * a traceback -- but "should" is not a guarantee, and this string goes on
     * screen, so strip anything address-shaped and cap the length first.
     */
    private fun safeReason(raw: String): String =
        ADDRESS.replace(raw.trim(), "[address hidden]").take(400)

    suspend fun list(context: Context): CalendarList = withContext(Dispatchers.IO) {
        val o = JSONObject(entry().callAttr("calendars_list", Brief.home(context)).toString())
        if (!o.optBoolean("ok")) {
            // Never emptyList(): dropping the reason here turns "could not be
            // read" into "none connected", and the user re-pastes an address
            // that is already on file.
            return@withContext CalendarList(
                error = o.optString("error").takeIf { it.isNotBlank() }?.let(::safeReason)
                    ?: "calendars.txt could not be read",
            )
        }
        val array = o.optJSONArray("calendars") ?: return@withContext CalendarList()
        CalendarList(
            entries = (0 until array.length()).map { i ->
                val c = array.optJSONObject(i)
                CalendarEntry(
                    label = c.optString("label"),
                    masked = c.optString("masked"),
                    isUrl = c.optBoolean("is_url"),
                )
            },
        )
    }

    suspend fun add(context: Context, label: String, target: String): Result<Unit> =
        withContext(Dispatchers.IO) {
            val o = JSONObject(
                entry().callAttr("calendars_add", Brief.home(context), label, target).toString(),
            )
            if (o.optBoolean("ok")) Result.success(Unit)
            else Result.failure(IllegalStateException(o.optString("error")))
        }

    suspend fun remove(context: Context, index: Int): Result<Unit> =
        withContext(Dispatchers.IO) {
            val o = JSONObject(
                entry().callAttr("calendars_remove", Brief.home(context), index).toString(),
            )
            if (o.optBoolean("ok")) Result.success(Unit)
            else Result.failure(IllegalStateException(o.optString("error")))
        }

    /** Actually fetches, so the result is what the morning run will get. */
    suspend fun check(context: Context): CalendarCheck = withContext(Dispatchers.IO) {
        val o = JSONObject(entry().callAttr("calendars_test", Brief.home(context)).toString())
        CalendarCheck(
            ok = o.optBoolean("ok"),
            status = o.optString("status"),
            reason = o.optString("reason"),
            events = o.optInt("events"),
        )
    }
}
