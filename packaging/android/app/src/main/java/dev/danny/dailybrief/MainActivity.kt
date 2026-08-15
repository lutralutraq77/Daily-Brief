package dev.danny.dailybrief

import android.Manifest
import android.os.Build
import android.os.Bundle
import android.webkit.WebView
import androidx.activity.ComponentActivity
import androidx.activity.compose.setContent
import androidx.activity.enableEdgeToEdge
import androidx.activity.result.contract.ActivityResultContracts
import androidx.compose.foundation.layout.Arrangement
import androidx.compose.foundation.layout.Box
import androidx.compose.foundation.layout.Column
import androidx.compose.foundation.layout.Spacer
import androidx.compose.foundation.layout.fillMaxSize
import androidx.compose.foundation.layout.fillMaxWidth
import androidx.compose.foundation.layout.height
import androidx.compose.foundation.layout.padding
import androidx.compose.foundation.layout.size
import androidx.compose.foundation.rememberScrollState
import androidx.compose.foundation.verticalScroll
import androidx.compose.material.icons.Icons
// Article, CalendarMonth and GridView need no import: they are vendored in
// Icons.kt, in this same package. Place and Refresh come from
// material-icons-core, which material3 already puts on the classpath.
import androidx.compose.material.icons.filled.Place
import androidx.compose.material.icons.filled.Refresh
import androidx.compose.material3.Button
import androidx.compose.material3.CircularProgressIndicator
import androidx.compose.material3.ExperimentalMaterial3Api
import androidx.compose.material3.Icon
import androidx.compose.material3.IconButton
import androidx.compose.material3.LinearProgressIndicator
import androidx.compose.material3.MaterialTheme
import androidx.compose.material3.NavigationBar
import androidx.compose.material3.NavigationBarItem
import androidx.compose.material3.OutlinedTextField
import androidx.compose.material3.Scaffold
import androidx.compose.material3.Text
import androidx.compose.material3.TopAppBar
import androidx.compose.runtime.Composable
import androidx.compose.runtime.LaunchedEffect
import androidx.compose.runtime.getValue
import androidx.compose.runtime.mutableStateOf
import androidx.compose.runtime.remember
import androidx.compose.runtime.rememberCoroutineScope
import androidx.compose.runtime.setValue
import androidx.compose.ui.Alignment
import androidx.compose.ui.Modifier
import androidx.compose.ui.graphics.vector.ImageVector
import androidx.compose.ui.platform.LocalContext
import androidx.compose.ui.text.style.TextAlign
import androidx.compose.ui.unit.dp
import androidx.compose.ui.viewinterop.AndroidView
import androidx.lifecycle.Lifecycle
import androidx.lifecycle.compose.LocalLifecycleOwner
import androidx.lifecycle.repeatOnLifecycle
import kotlinx.coroutines.launch
import java.io.File

class MainActivity : ComponentActivity() {

    private val notificationPermission = registerForActivityResult(
        ActivityResultContracts.RequestPermission(),
    ) { /* Declining only means no "brief is ready" nudge. */ }

    override fun onCreate(savedInstanceState: Bundle?) {
        super.onCreate(savedInstanceState)
        enableEdgeToEdge()
        setContent { DailyBriefTheme { RootScreen() } }

        if (Build.VERSION.SDK_INT >= Build.VERSION_CODES.TIRAMISU) {
            notificationPermission.launch(Manifest.permission.POST_NOTIFICATIONS)
        }
    }
}

private enum class Tab(val label: String, val icon: ImageVector) {
    BRIEF("Brief", Icons.Default.Article),
    PLAN("3×3", Icons.Default.GridView),
    CALENDARS("Calendars", Icons.Default.CalendarMonth),
}

@OptIn(ExperimentalMaterial3Api::class)
@Composable
private fun RootScreen() {
    val context = LocalContext.current
    val scope = rememberCoroutineScope()

    var tab by remember { mutableStateOf(Tab.BRIEF) }
    var status by remember { mutableStateOf(BriefStatus()) }
    var loading by remember { mutableStateOf(true) }
    var running by remember { mutableStateOf(false) }
    var message by remember { mutableStateOf<String?>(null) }
    // "Busy" and "updated" are both messages, and neither is an error, so the
    // message alone cannot decide its own colour.
    var messageIsError by remember { mutableStateOf(false) }
    var editingCity by remember { mutableStateOf(false) }
    var cityInput by remember { mutableStateOf("") }

    suspend fun refreshStatus() {
        status = Brief.status(context)
        cityInput = status.city
    }

    LaunchedEffect(Unit) {
        Brief.seedConfig(context)
        refreshStatus()
        loading = false
        editingCity = status.city.isBlank()
        if (!editingCity) Scheduler.scheduleDaily(context)
    }

    // A composition survives onStop/onStart, so LaunchedEffect(Unit) above runs
    // once and never again -- returning via Recents after the 08:00 worker ran
    // would keep showing yesterday's brief. seedConfig, the loading gate and
    // scheduleDaily deliberately stay in that effect so a resume never flashes
    // the spinner or re-enqueues work.
    val lifecycle = LocalLifecycleOwner.current.lifecycle
    LaunchedEffect(lifecycle) {
        lifecycle.repeatOnLifecycle(Lifecycle.State.RESUMED) {
            // Don't clobber an in-flight inline run: refreshStatus would briefly
            // publish the PREVIOUS brief's generatedAt and flip the WebView back.
            if (!running) refreshStatus()
        }
    }

    fun generate() {
        if (running) return
        running = true
        message = null
        messageIsError = false
        tab = Tab.BRIEF
        scope.launch {
            val result = Brief.run(context)
            running = false
            // Busy is checked first and on its own: it arrives as ok=false but
            // nothing failed, so the brief already on screen stays exactly as it
            // is and the user is told what is actually happening.
            messageIsError = !result.ok && !result.busy
            message = when {
                result.busy -> "A brief is already being generated."
                // A successful run needs no strip under the brief: the brief
                // itself ends with a "Sources N/N OK" line, so printing the raw
                // per-section statuses (bankholiday=empty, calendar=empty, ...)
                // repeated that information and permanently ate screen space.
                // Failures still surface here, which is what the strip is for.
                result.ok -> null
                else -> result.error?.lines()?.lastOrNull { it.isNotBlank() }
                    ?: "The brief could not be generated"
            }
            refreshStatus()
        }
    }

    Scaffold(
        topBar = {
            TopAppBar(
                title = {
                    Text(
                        when (tab) {
                            Tab.BRIEF ->
                                if (status.city.isNotBlank()) "Daily Brief · ${status.city}"
                                else "Daily Brief"
                            Tab.PLAN -> "The 3×3 plan"
                            Tab.CALENDARS -> "Calendars"
                        },
                    )
                },
                actions = {
                    if (tab == Tab.BRIEF) {
                        IconButton(onClick = { editingCity = true }) {
                            Icon(Icons.Default.Place, contentDescription = "Change location")
                        }
                    }
                    // This is the refresh that works. The in-page one was an
                    // <a href="dailybrief:refresh"> relying on the protocol
                    // handler firing from inside the WebView, which did not
                    // reliably regenerate; this calls generate() directly. The
                    // page-level bar is omitted on Android so there is exactly
                    // one refresh on screen.
                    IconButton(onClick = { generate() }, enabled = !running) {
                        Icon(Icons.Default.Refresh, contentDescription = "Generate now")
                    }
                },
            )
        },
        bottomBar = {
            NavigationBar {
                Tab.entries.forEach { entry ->
                    NavigationBarItem(
                        selected = tab == entry,
                        onClick = { tab = entry },
                        icon = { Icon(entry.icon, contentDescription = null) },
                        label = { Text(entry.label) },
                    )
                }
            }
        },
    ) { padding ->
        Box(modifier = Modifier.fillMaxSize().padding(padding)) {
            when {
                loading -> Box(Modifier.fillMaxSize(), Alignment.Center) { CircularProgressIndicator() }

                editingCity -> CityPrompt(
                    city = cityInput,
                    onCityChange = { cityInput = it },
                    onSave = {
                        scope.launch {
                            Brief.setCity(context, cityInput)
                            editingCity = false
                            refreshStatus()
                            Scheduler.scheduleDaily(context)
                            generate()
                        }
                    },
                )

                tab == Tab.PLAN -> PlanScreen()
                tab == Tab.CALENDARS -> CalendarsScreen()

                status.hasBrief -> Column(Modifier.fillMaxSize()) {
                    if (running) LinearProgressIndicator(Modifier.fillMaxWidth())
                    BriefView(
                        path = status.latest,
                        reloadKey = status.generatedAt,
                        modifier = Modifier.weight(1f),
                    )
                    message?.let {
                        Text(
                            text = it,
                            style = MaterialTheme.typography.bodySmall,
                            color = MaterialTheme.colorScheme.onSurfaceVariant,
                            modifier = Modifier.fillMaxWidth().padding(12.dp),
                        )
                    }
                }

                else -> NoBriefYet(
                    running = running,
                    message = message,
                    messageIsError = messageIsError,
                    onGenerate = { generate() },
                )
            }
        }
    }
}

@Composable
private fun CityPrompt(city: String, onCityChange: (String) -> Unit, onSave: () -> Unit) {
    Column(
        modifier = Modifier.fillMaxSize().verticalScroll(rememberScrollState()).padding(24.dp),
    ) {
        Text("Where are you?", style = MaterialTheme.typography.headlineSmall)
        Text(
            text = "Used for the weather and for sunrise and sunset. Nothing else needs it, " +
                "and it never leaves the device except as a place-name lookup.",
            style = MaterialTheme.typography.bodyMedium,
            color = MaterialTheme.colorScheme.onSurfaceVariant,
            modifier = Modifier.padding(top = 8.dp),
        )
        Spacer(Modifier.height(24.dp))
        OutlinedTextField(
            value = city,
            onValueChange = onCityChange,
            label = { Text("City") },
            singleLine = true,
            modifier = Modifier.fillMaxWidth(),
        )
        Spacer(Modifier.height(20.dp))
        Button(onClick = onSave, enabled = city.isNotBlank(), modifier = Modifier.fillMaxWidth()) {
            Text("Save and generate")
        }
    }
}

@Composable
private fun NoBriefYet(
    running: Boolean,
    message: String?,
    messageIsError: Boolean,
    onGenerate: () -> Unit,
) {
    Column(
        modifier = Modifier.fillMaxSize().padding(32.dp),
        horizontalAlignment = Alignment.CenterHorizontally,
        verticalArrangement = Arrangement.Center,
    ) {
        Text(
            text = if (running) "Gathering your brief…" else "No brief yet",
            style = MaterialTheme.typography.titleMedium,
            textAlign = TextAlign.Center,
        )
        message?.let {
            Text(
                text = it,
                style = MaterialTheme.typography.bodySmall,
                color = if (messageIsError) {
                    MaterialTheme.colorScheme.error
                } else {
                    MaterialTheme.colorScheme.onSurfaceVariant
                },
                textAlign = TextAlign.Center,
                modifier = Modifier.padding(top = 12.dp),
            )
        }
        Spacer(Modifier.height(20.dp))
        if (running) {
            CircularProgressIndicator(Modifier.size(28.dp))
        } else {
            Button(onClick = onGenerate) { Text("Generate now") }
        }
    }
}

/** The brief is a self-contained HTML page, so it renders as-is. */
@Composable
private fun BriefView(path: String, reloadKey: Long, modifier: Modifier = Modifier) {
    AndroidView(
        modifier = modifier,
        factory = { ctx ->
            WebView(ctx).apply {
                settings.allowFileAccess = true
                settings.javaScriptEnabled = false
                setBackgroundColor(android.graphics.Color.TRANSPARENT)
            }
        },
        update = { view ->
            if (path.isNotBlank() && File(path).exists()) {
                view.loadUrl("file://$path?v=$reloadKey")
            }
        },
    )
}
