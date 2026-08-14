package dev.danny.dailybrief

import android.content.Context
import com.chaquo.python.PyObject
import com.chaquo.python.Python
import kotlinx.coroutines.Dispatchers
import kotlinx.coroutines.withContext
import org.json.JSONObject
import java.io.File

data class BriefStatus(
    val configured: Boolean = false,
    val city: String = "",
    val hasBrief: Boolean = false,
    val latest: String = "",
    val generatedAt: Long = 0L,
)

/**
 * [busy] is a third state, not a failure: another run already holds the Python
 * side's lock, so nothing was generated and nothing went wrong. It arrives with
 * ok=false, so every consumer must test it before deciding "could not generate".
 */
data class RunResult(
    val ok: Boolean,
    val busy: Boolean = false,
    val sections: String = "",
    val latest: String = "",
    val error: String? = null,
)

/**
 * The bridge to the collectors.
 *
 * Everything below this line is the same Python the Windows and Arch builds
 * run. Nothing about the brief is reimplemented here -- this object only tells
 * Python where to write and turns its JSON back into Kotlin.
 */
object Brief {

    /** App-private storage: config.json, briefs/ and logs/ live here. */
    fun home(context: Context): String =
        File(context.filesDir, "data").apply { mkdirs() }.absolutePath

    private fun entry(): PyObject = Python.getInstance().getModule("android_entry")

    private fun json(value: PyObject): JSONObject = JSONObject(value.toString())

    suspend fun status(context: Context): BriefStatus = withContext(Dispatchers.IO) {
        val o = json(entry().callAttr("status", home(context)))
        BriefStatus(
            configured = o.optBoolean("configured"),
            city = o.optString("city"),
            hasBrief = o.optBoolean("has_brief"),
            latest = o.optString("latest"),
            generatedAt = (o.optDouble("generated_at", 0.0) * 1000).toLong(),
        )
    }

    /**
     * Creates a config on first launch. It is deliberately empty: Python's
     * DEFAULT_CONFIG supplies the feeds, sections and units, so there is only
     * ever one set of defaults across all three platforms.
     */
    suspend fun seedConfig(context: Context): Boolean = withContext(Dispatchers.IO) {
        entry().callAttr("seed_config", home(context)).toBoolean()
    }

    suspend fun setCity(context: Context, city: String): Unit = withContext(Dispatchers.IO) {
        entry().callAttr("set_location", home(context), city, "")
        Unit
    }

    suspend fun run(context: Context): RunResult = withContext(Dispatchers.IO) {
        val o = json(entry().callAttr("run_brief", home(context), true))
        RunResult(
            ok = o.optBoolean("ok"),
            busy = o.optBoolean("busy"),
            sections = o.optString("sections"),
            latest = o.optString("latest"),
            error = if (o.has("error")) o.optString("error") else null,
        )
    }
}
