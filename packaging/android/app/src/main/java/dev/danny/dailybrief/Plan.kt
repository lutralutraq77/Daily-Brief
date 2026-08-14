package dev.danny.dailybrief

import android.content.Context
import com.chaquo.python.Python
import kotlinx.coroutines.Dispatchers
import kotlinx.coroutines.withContext
import org.json.JSONArray
import org.json.JSONObject

data class PlanSource(val title: String = "", val url: String = "") {
    /** The "Title|url" form plan.py's set_topic parses. */
    fun encode(): String = when {
        title.isBlank() && url.isBlank() -> ""
        url.isBlank() -> title.trim()
        else -> "${title.trim()}|${url.trim()}"
    }
}

data class PlanTopic(
    val name: String = "",
    val video: PlanSource = PlanSource(),
    val text: PlanSource = PlanSource(),
    val misc: PlanSource = PlanSource(),
    val outputDone: Boolean = false,
)

data class PlanBlock(
    val number: Int = 0,
    val topicCount: Int = 0,
    val name: String = "",
    val week: Int = 0,
    val due: String = "",
    val monthStart: String = "",
    val monthEnd: String = "",
    val daysLeft: Int = 0,
    val outputDone: Boolean = false,
    val missing: List<String> = emptyList(),
    // plan.py sends a stub block in the not_started and finished states, whose
    // only real fields are these two and topic_count. Dropping them left the UI
    // rendering "week 0 · 0 days left" off the zero defaults.
    val starts: String = "",
    val ended: String = "",
)

data class PlanWeekly(
    val changes: List<String> = emptyList(),
    val verdicts: List<String> = emptyList(),
    val weekOf: String = "",
    val reviewDay: String = "sunday",
    val reviewDue: Boolean = false,
    val stale: Boolean = false,
    val daysRunning: Int = 0,
)

data class PlanState(
    val ok: Boolean = false,
    val exists: Boolean = false,
    val error: String? = null,
    val blockState: String = "",
    val block: PlanBlock? = null,
    val weekly: PlanWeekly = PlanWeekly(),
    val topics: List<PlanTopic> = emptyList(),
    val start: String = "",
    val reviewDay: String = "sunday",
    val sessionMinutes: Int = 45,
    val problems: List<String> = emptyList(),
    val weekdays: List<String> = emptyList(),
    val verdictOptions: List<String> = emptyList(),
)

/**
 * The 3x3 plan, edited on the phone.
 *
 * Every rule -- which source a week is due, when a weekly set has gone stale,
 * how month blocks clamp to the target month's length -- stays in plan.py. This
 * file only moves values across the bridge.
 */
object Plan {

    private fun entry() = Python.getInstance().getModule("android_entry")

    private fun strings(array: JSONArray?): List<String> =
        (0 until (array?.length() ?: 0)).map { array!!.optString(it) }

    private fun source(o: JSONObject?): PlanSource =
        if (o == null) PlanSource() else PlanSource(o.optString("title"), o.optString("url"))

    suspend fun state(context: Context): PlanState = withContext(Dispatchers.IO) {
        val o = JSONObject(entry().callAttr("plan_state", Brief.home(context)).toString())
        if (!o.optBoolean("ok")) {
            return@withContext PlanState(
                ok = false,
                exists = o.optBoolean("exists"),
                error = o.optString("error").takeIf { it.isNotBlank() },
            )
        }

        val status = o.optJSONObject("status") ?: JSONObject()
        val raw = o.optJSONObject("plan") ?: JSONObject()

        val blockJson = status.optJSONObject("block")
        val block = blockJson?.let {
            val sources = it.optJSONObject("sources") ?: JSONObject()
            PlanBlock(
                number = it.optInt("number"),
                topicCount = it.optInt("topic_count"),
                name = it.optString("name"),
                week = it.optInt("week"),
                due = it.optString("due"),
                monthStart = it.optString("month_start"),
                monthEnd = it.optString("month_end"),
                daysLeft = it.optInt("days_left"),
                outputDone = it.optBoolean("output_done"),
                missing = strings(it.optJSONArray("missing")).ifEmpty {
                    // `missing` may arrive as slot names only; keep whatever came.
                    strings(sources.names())
                        .filter { slot -> sources.optJSONObject(slot) == null }
                },
                starts = it.optString("starts"),
                ended = it.optString("ended"),
            )
        }

        val weeklyJson = status.optJSONObject("weekly") ?: JSONObject()
        val rawWeekly = raw.optJSONObject("weekly") ?: JSONObject()

        val topicsArray = raw.optJSONArray("topics") ?: JSONArray()
        val topics = (0 until topicsArray.length()).map { i ->
            val t = topicsArray.optJSONObject(i) ?: JSONObject()
            val s = t.optJSONObject("sources") ?: JSONObject()
            PlanTopic(
                name = t.optString("name"),
                video = source(s.optJSONObject("video")),
                text = source(s.optJSONObject("text")),
                misc = source(s.optJSONObject("misc")),
                outputDone = t.optJSONObject("output")?.optBoolean("done") ?: false,
            )
        }

        PlanState(
            ok = true,
            exists = o.optBoolean("exists"),
            blockState = status.optString("block_state"),
            block = block,
            weekly = PlanWeekly(
                changes = strings(weeklyJson.optJSONArray("changes")),
                verdicts = strings(rawWeekly.optJSONArray("verdicts")),
                weekOf = weeklyJson.optString("week_of"),
                reviewDay = weeklyJson.optString("review_day").ifBlank { "sunday" },
                reviewDue = weeklyJson.optBoolean("review_due"),
                stale = weeklyJson.optBoolean("stale"),
                daysRunning = weeklyJson.optInt("days_running"),
            ),
            topics = topics,
            start = raw.optString("start"),
            reviewDay = raw.optString("review_day").ifBlank { "sunday" },
            sessionMinutes = raw.optInt("session_minutes", 45),
            problems = strings(status.optJSONArray("problems")),
            weekdays = strings(o.optJSONArray("weekdays")),
            verdictOptions = strings(o.optJSONArray("verdicts")),
        )
    }

    private fun result(json: String): Result<String> {
        val o = JSONObject(json)
        return if (o.optBoolean("ok")) {
            Result.success(o.optString("message"))
        } else {
            Result.failure(IllegalStateException(o.optString("error").ifBlank { "Unknown error" }))
        }
    }

    suspend fun init(context: Context, startIso: String): Result<String> =
        withContext(Dispatchers.IO) {
            result(entry().callAttr("plan_init", Brief.home(context), startIso).toString())
        }

    suspend fun setTopic(context: Context, number: Int, topic: PlanTopic): Result<String> =
        withContext(Dispatchers.IO) {
            result(
                entry().callAttr(
                    "plan_set_topic", Brief.home(context), number, topic.name,
                    topic.video.encode(), topic.text.encode(), topic.misc.encode(),
                ).toString(),
            )
        }

    suspend fun setWeek(context: Context, a: String, b: String, c: String): Result<String> =
        withContext(Dispatchers.IO) {
            result(entry().callAttr("plan_set_week", Brief.home(context), a, b, c).toString())
        }

    suspend fun scoreWeek(context: Context, verdicts: List<String>): Result<String> =
        withContext(Dispatchers.IO) {
            result(
                entry().callAttr(
                    "plan_score_week", Brief.home(context), verdicts.joinToString(","),
                ).toString(),
            )
        }

    suspend fun markOutput(context: Context, note: String): Result<String> =
        withContext(Dispatchers.IO) {
            result(entry().callAttr("plan_mark_output", Brief.home(context), note).toString())
        }

    suspend fun setSettings(
        context: Context,
        startIso: String = "",
        reviewDay: String = "",
        sessionMinutes: Int = 0,
    ): Result<String> = withContext(Dispatchers.IO) {
        result(
            entry().callAttr(
                "plan_set_settings", Brief.home(context), startIso, reviewDay,
                sessionMinutes, "", "",
            ).toString(),
        )
    }
}
