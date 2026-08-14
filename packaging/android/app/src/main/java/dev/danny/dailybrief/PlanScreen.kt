package dev.danny.dailybrief

import androidx.compose.foundation.layout.Arrangement
import androidx.compose.foundation.layout.Box
import androidx.compose.foundation.layout.Column
import androidx.compose.foundation.layout.Row
import androidx.compose.foundation.layout.Spacer
import androidx.compose.foundation.layout.fillMaxSize
import androidx.compose.foundation.layout.fillMaxWidth
import androidx.compose.foundation.layout.height
import androidx.compose.foundation.layout.padding
import androidx.compose.foundation.rememberScrollState
import androidx.compose.foundation.verticalScroll
import androidx.compose.material3.Button
import androidx.compose.material3.Card
import androidx.compose.material3.CardDefaults
import androidx.compose.material3.CircularProgressIndicator
import androidx.compose.material3.DropdownMenu
import androidx.compose.material3.DropdownMenuItem
import androidx.compose.material3.FilterChip
import androidx.compose.material3.HorizontalDivider
import androidx.compose.material3.MaterialTheme
import androidx.compose.material3.OutlinedButton
import androidx.compose.material3.OutlinedTextField
import androidx.compose.material3.Text
import androidx.compose.material3.TextButton
import androidx.compose.runtime.Composable
import androidx.compose.runtime.LaunchedEffect
import androidx.compose.runtime.getValue
import androidx.compose.runtime.mutableStateListOf
import androidx.compose.runtime.mutableStateOf
import androidx.compose.runtime.remember
import androidx.compose.runtime.rememberCoroutineScope
import androidx.compose.runtime.setValue
import androidx.compose.ui.Alignment
import androidx.compose.ui.Modifier
import androidx.compose.ui.platform.LocalContext
import androidx.compose.ui.text.font.FontWeight
import androidx.compose.ui.unit.dp
import kotlinx.coroutines.launch
import java.time.LocalDate

private val SLOT_HELP = mapOf(
    "video" to "Lecture or talk",
    "text" to "Article, book or long-form journalism",
    "misc" to "Course, series or anything else",
)

@Composable
fun PlanScreen(modifier: Modifier = Modifier) {
    val context = LocalContext.current
    val scope = rememberCoroutineScope()

    var state by remember { mutableStateOf(PlanState()) }
    var loading by remember { mutableStateOf(true) }
    var message by remember { mutableStateOf<String?>(null) }
    var error by remember { mutableStateOf<String?>(null) }

    suspend fun reload() {
        state = Plan.state(context)
        loading = false
    }

    LaunchedEffect(Unit) { reload() }

    fun act(block: suspend () -> Result<String>) {
        scope.launch {
            message = null
            error = null
            block()
                .onSuccess { message = it.ifBlank { "Saved" } }
                .onFailure { error = it.message }
            reload()
        }
    }

    if (loading) {
        Box(modifier.fillMaxSize(), Alignment.Center) { CircularProgressIndicator() }
        return
    }

    Column(
        modifier = modifier
            .fillMaxSize()
            .verticalScroll(rememberScrollState())
            .padding(16.dp),
        verticalArrangement = Arrangement.spacedBy(16.dp),
    ) {
        // A plan.json we could not parse is reported, never quietly replaced.
        // Test !ok FIRST and unconditionally: two of plan_state's three failure
        // returns carry no `exists` key at all, and a missing key reads as
        // false, which used to send a broken plan down the onboarding path and
        // throw the reason away.
        if (!state.ok) {
            Card(colors = CardDefaults.cardColors(containerColor = MaterialTheme.colorScheme.errorContainer)) {
                Column(Modifier.padding(16.dp)) {
                    Text(
                        if (state.exists) "plan.json cannot be read"
                        else "the 3×3 plan could not be loaded",
                        style = MaterialTheme.typography.titleSmall,
                    )
                    Text(
                        state.error ?: "no reason was given",
                        style = MaterialTheme.typography.bodySmall,
                        modifier = Modifier.padding(top = 6.dp),
                    )
                }
            }
            return@Column
        }

        if (!state.exists) {
            StartPlanCard(onStart = { iso -> act { Plan.init(context, iso) } })
            return@Column
        }

        StatusCard(state)
        WeeklyCard(state, onFile = { a, b, c -> act { Plan.setWeek(context, a, b, c) } },
            onScore = { v -> act { Plan.scoreWeek(context, v) } })
        // Local val: `state` is a delegated property, so Kotlin cannot smart cast
        // state.block across the two reads.
        val activeBlock = state.block
        if (activeBlock != null && state.blockState == "active" && !activeBlock.outputDone) {
            OutputCard(activeBlock) { note -> act { Plan.markOutput(context, note) } }
        }
        TopicsCard(state) { number, topic -> act { Plan.setTopic(context, number, topic) } }
        SettingsCard(state) { start, day, minutes ->
            act { Plan.setSettings(context, start, day, minutes) }
        }

        message?.let { Text(it, color = MaterialTheme.colorScheme.primary) }
        error?.let { Text(it, color = MaterialTheme.colorScheme.error) }
        Spacer(Modifier.height(24.dp))
    }
}

@Composable
private fun StartPlanCard(onStart: (String) -> Unit) {
    var start by remember {
        mutableStateOf(LocalDate.now().withDayOfMonth(1).plusMonths(1).toString())
    }
    Card {
        Column(Modifier.padding(16.dp)) {
            Text("Start a 3×3 plan", style = MaterialTheme.typography.titleMedium)
            Text(
                "Three topics, one calendar month each. Week 1 is the video, week 2 the text, " +
                    "week 3 the misc source, week 4 is producing something.",
                style = MaterialTheme.typography.bodySmall,
                color = MaterialTheme.colorScheme.onSurfaceVariant,
                modifier = Modifier.padding(top = 6.dp),
            )
            Spacer(Modifier.height(12.dp))
            OutlinedTextField(
                value = start,
                onValueChange = { start = it },
                label = { Text("First block starts (YYYY-MM-DD)") },
                singleLine = true,
                modifier = Modifier.fillMaxWidth(),
            )
            Spacer(Modifier.height(12.dp))
            Button(onClick = { onStart(start) }, modifier = Modifier.fillMaxWidth()) {
                Text("Create the plan")
            }
        }
    }
}

@Composable
private fun StatusCard(state: PlanState) {
    Card {
        Column(Modifier.padding(16.dp)) {
            Text("Where you are", style = MaterialTheme.typography.titleMedium)
            Spacer(Modifier.height(8.dp))

            val block = state.block
            // plan.py emits exactly unset / not_started / active / finished.
            // Branching on the state, not on block == null, because the two
            // non-active states still send a stub block -- so a null test picked
            // the detail branch and rendered "week 0 · 0 days left".
            when (state.blockState) {
                "not_started" ->
                    Text("The first block starts ${block?.starts.orEmpty().ifBlank { state.start }}.")
                "finished" ->
                    Text(
                        "Block finished ${block?.ended.orEmpty()}. " +
                            "Set a new start and ${block?.topicCount ?: 3} new topics.",
                    )
                "active" -> if (block == null) {
                    Text(
                        "A block is running, but its detail could not be read.",
                        style = MaterialTheme.typography.bodyMedium,
                    )
                } else {
                    Text(
                        text = block.name.ifBlank { "Topic ${block.number} has no name yet" },
                        style = MaterialTheme.typography.titleLarge,
                        fontWeight = FontWeight.Bold,
                    )
                    Text(
                        "Topic ${block.number} of ${block.topicCount} · week ${block.week} · " +
                            "${block.daysLeft} days left",
                        style = MaterialTheme.typography.bodySmall,
                        color = MaterialTheme.colorScheme.onSurfaceVariant,
                    )
                    Spacer(Modifier.height(8.dp))
                    Text(
                        text = when (block.due) {
                            "output" -> "This week: produce the output."
                            "" -> "Nothing due this week."
                            else -> "This week: the ${block.due} source."
                        },
                        style = MaterialTheme.typography.bodyLarge,
                    )
                    if (block.missing.isNotEmpty()) {
                        Text(
                            "Not filled in yet: ${block.missing.joinToString(", ")}",
                            style = MaterialTheme.typography.bodySmall,
                            color = MaterialTheme.colorScheme.error,
                            modifier = Modifier.padding(top = 6.dp),
                        )
                    }
                }
                // "unset", and anything plan.py might add later.
                else ->
                    Text(
                        "No month-block is running. Add topics below, or change the start date.",
                        style = MaterialTheme.typography.bodyMedium,
                    )
            }

            state.problems.forEach {
                Text(
                    "• $it",
                    style = MaterialTheme.typography.bodySmall,
                    color = MaterialTheme.colorScheme.error,
                    modifier = Modifier.padding(top = 6.dp),
                )
            }
        }
    }
}

@Composable
private fun WeeklyCard(
    state: PlanState,
    onFile: (String, String, String) -> Unit,
    onScore: (List<String>) -> Unit,
) {
    val current = state.weekly.changes
    val fields = remember(current) {
        mutableStateListOf(
            current.getOrElse(0) { "" },
            current.getOrElse(1) { "" },
            current.getOrElse(2) { "" },
        )
    }
    val verdicts = remember(state.weekly.verdicts, current) {
        mutableStateListOf(
            state.weekly.verdicts.getOrElse(0) { "" },
            state.weekly.verdicts.getOrElse(1) { "" },
            state.weekly.verdicts.getOrElse(2) { "" },
        )
    }

    Card {
        Column(Modifier.padding(16.dp)) {
            Text("This week's three changes", style = MaterialTheme.typography.titleMedium)
            if (state.weekly.stale) {
                Text(
                    "These are stale — they were filed for the week of ${state.weekly.weekOf}. " +
                        "Pick this week's set.",
                    style = MaterialTheme.typography.bodySmall,
                    color = MaterialTheme.colorScheme.error,
                    modifier = Modifier.padding(top = 4.dp),
                )
            } else if (state.weekly.reviewDue) {
                Text(
                    "Review day. Score last week, then file three new changes.",
                    style = MaterialTheme.typography.bodySmall,
                    color = MaterialTheme.colorScheme.primary,
                    modifier = Modifier.padding(top = 4.dp),
                )
            } else if (current.isNotEmpty()) {
                Text(
                    "Running ${state.weekly.daysRunning} day(s), since ${state.weekly.weekOf}.",
                    style = MaterialTheme.typography.bodySmall,
                    color = MaterialTheme.colorScheme.onSurfaceVariant,
                    modifier = Modifier.padding(top = 4.dp),
                )
            }

            Spacer(Modifier.height(8.dp))
            fields.forEachIndexed { i, value ->
                OutlinedTextField(
                    value = value,
                    onValueChange = { fields[i] = it },
                    label = { Text("Change ${i + 1}") },
                    singleLine = true,
                    modifier = Modifier.fillMaxWidth().padding(vertical = 4.dp),
                )
            }
            Button(
                onClick = { onFile(fields[0], fields[1], fields[2]) },
                modifier = Modifier.fillMaxWidth().padding(top = 8.dp),
            ) { Text("File these for this week") }

            if (current.isNotEmpty()) {
                HorizontalDivider(Modifier.padding(vertical = 14.dp))
                Text("How did they go?", style = MaterialTheme.typography.titleSmall)
                current.forEachIndexed { i, change ->
                    Text(
                        change,
                        style = MaterialTheme.typography.bodyMedium,
                        modifier = Modifier.padding(top = 10.dp),
                    )
                    Row(horizontalArrangement = Arrangement.spacedBy(8.dp)) {
                        state.verdictOptions.forEach { option ->
                            FilterChip(
                                selected = verdicts.getOrElse(i) { "" } == option,
                                onClick = { if (i < verdicts.size) verdicts[i] = option },
                                label = { Text(option) },
                            )
                        }
                    }
                }
                OutlinedButton(
                    onClick = { onScore(verdicts.take(current.size).filter { it.isNotBlank() }) },
                    enabled = verdicts.take(current.size).all { it.isNotBlank() },
                    modifier = Modifier.fillMaxWidth().padding(top = 12.dp),
                ) { Text("Score them") }
            }
        }
    }
}

@Composable
private fun OutputCard(block: PlanBlock, onMark: (String) -> Unit) {
    var note by remember { mutableStateOf("") }
    Card {
        Column(Modifier.padding(16.dp)) {
            Text("Output for ${block.name.ifBlank { "this topic" }}",
                style = MaterialTheme.typography.titleMedium)
            Text(
                "Week 4 is producing something — a voice memo, a page of notes, an explanation " +
                    "to someone else.",
                style = MaterialTheme.typography.bodySmall,
                color = MaterialTheme.colorScheme.onSurfaceVariant,
                modifier = Modifier.padding(top = 6.dp),
            )
            OutlinedTextField(
                value = note,
                onValueChange = { note = it },
                label = { Text("What did you produce?") },
                modifier = Modifier.fillMaxWidth().padding(top = 10.dp),
            )
            Button(
                onClick = { onMark(note) },
                modifier = Modifier.fillMaxWidth().padding(top = 10.dp),
            ) { Text("Mark it produced") }
        }
    }
}

@Composable
private fun TopicsCard(state: PlanState, onSave: (Int, PlanTopic) -> Unit) {
    Card {
        Column(Modifier.padding(16.dp)) {
            Text("Topics", style = MaterialTheme.typography.titleMedium)
            Text(
                "One calendar month each. Make at least one unrelated to your work.",
                style = MaterialTheme.typography.bodySmall,
                color = MaterialTheme.colorScheme.onSurfaceVariant,
            )
            val count = maxOf(3, state.topics.size)
            (0 until count).forEach { i ->
                TopicEditor(
                    number = i + 1,
                    initial = state.topics.getOrElse(i) { PlanTopic() },
                    onSave = { onSave(i + 1, it) },
                )
                if (i < count - 1) HorizontalDivider(Modifier.padding(vertical = 12.dp))
            }
        }
    }
}

@Composable
private fun TopicEditor(number: Int, initial: PlanTopic, onSave: (PlanTopic) -> Unit) {
    var expanded by remember { mutableStateOf(false) }
    var name by remember(initial) { mutableStateOf(initial.name) }
    var videoTitle by remember(initial) { mutableStateOf(initial.video.title) }
    var videoUrl by remember(initial) { mutableStateOf(initial.video.url) }
    var textTitle by remember(initial) { mutableStateOf(initial.text.title) }
    var textUrl by remember(initial) { mutableStateOf(initial.text.url) }
    var miscTitle by remember(initial) { mutableStateOf(initial.misc.title) }
    var miscUrl by remember(initial) { mutableStateOf(initial.misc.url) }

    Column(Modifier.padding(top = 12.dp)) {
        Row(verticalAlignment = Alignment.CenterVertically) {
            Column(Modifier.weight(1f)) {
                Text(
                    text = name.ifBlank { "Topic $number — not set" },
                    style = MaterialTheme.typography.titleSmall,
                )
                if (initial.outputDone) {
                    Text(
                        "Output produced",
                        style = MaterialTheme.typography.bodySmall,
                        color = MaterialTheme.colorScheme.primary,
                    )
                }
            }
            TextButton(onClick = { expanded = !expanded }) {
                Text(if (expanded) "Close" else "Edit")
            }
        }

        if (expanded) {
            OutlinedTextField(
                value = name,
                onValueChange = { name = it },
                label = { Text("Topic name") },
                singleLine = true,
                modifier = Modifier.fillMaxWidth().padding(top = 6.dp),
            )
            SourceFields("video", videoTitle, videoUrl, { videoTitle = it }, { videoUrl = it })
            SourceFields("text", textTitle, textUrl, { textTitle = it }, { textUrl = it })
            SourceFields("misc", miscTitle, miscUrl, { miscTitle = it }, { miscUrl = it })
            Button(
                onClick = {
                    onSave(
                        PlanTopic(
                            name = name,
                            video = PlanSource(videoTitle, videoUrl),
                            text = PlanSource(textTitle, textUrl),
                            misc = PlanSource(miscTitle, miscUrl),
                        ),
                    )
                    expanded = false
                },
                modifier = Modifier.fillMaxWidth().padding(top = 10.dp),
            ) { Text("Save topic $number") }
        }
    }
}

@Composable
private fun SourceFields(
    slot: String,
    title: String,
    url: String,
    onTitle: (String) -> Unit,
    onUrl: (String) -> Unit,
) {
    Text(
        "${slot.replaceFirstChar { it.uppercase() }} — ${SLOT_HELP[slot]}",
        style = MaterialTheme.typography.labelMedium,
        color = MaterialTheme.colorScheme.onSurfaceVariant,
        modifier = Modifier.padding(top = 12.dp),
    )
    OutlinedTextField(
        value = title,
        onValueChange = onTitle,
        label = { Text("Title") },
        singleLine = true,
        modifier = Modifier.fillMaxWidth().padding(top = 4.dp),
    )
    OutlinedTextField(
        value = url,
        onValueChange = onUrl,
        label = { Text("Link (optional)") },
        singleLine = true,
        modifier = Modifier.fillMaxWidth().padding(top = 4.dp),
    )
}

@Composable
private fun SettingsCard(state: PlanState, onSave: (String, String, Int) -> Unit) {
    var start by remember(state.start) { mutableStateOf(state.start) }
    var reviewDay by remember(state.reviewDay) { mutableStateOf(state.reviewDay) }
    var minutes by remember(state.sessionMinutes) { mutableStateOf(state.sessionMinutes.toString()) }
    var dayMenu by remember { mutableStateOf(false) }

    Card {
        Column(Modifier.padding(16.dp)) {
            Text("Plan settings", style = MaterialTheme.typography.titleMedium)
            OutlinedTextField(
                value = start,
                onValueChange = { start = it },
                label = { Text("First block starts (YYYY-MM-DD)") },
                singleLine = true,
                modifier = Modifier.fillMaxWidth().padding(top = 8.dp),
            )

            Box {
                OutlinedButton(
                    onClick = { dayMenu = true },
                    modifier = Modifier.fillMaxWidth().padding(top = 8.dp),
                ) { Text("Review day: ${reviewDay.replaceFirstChar { it.uppercase() }}") }
                DropdownMenu(expanded = dayMenu, onDismissRequest = { dayMenu = false }) {
                    state.weekdays.forEach { day ->
                        DropdownMenuItem(
                            text = { Text(day.replaceFirstChar { it.uppercase() }) },
                            onClick = {
                                reviewDay = day
                                dayMenu = false
                            },
                        )
                    }
                }
            }

            OutlinedTextField(
                value = minutes,
                onValueChange = { minutes = it.filter(Char::isDigit) },
                label = { Text("Session length (minutes)") },
                singleLine = true,
                modifier = Modifier.fillMaxWidth().padding(top = 8.dp),
            )
            Text(
                "The brief looks for a gap this long in your calendar. Connect a calendar and " +
                    "it will only ever offer time that is genuinely free.",
                style = MaterialTheme.typography.bodySmall,
                color = MaterialTheme.colorScheme.onSurfaceVariant,
                modifier = Modifier.padding(top = 6.dp),
            )
            Button(
                onClick = { onSave(start, reviewDay, minutes.toIntOrNull() ?: 0) },
                modifier = Modifier.fillMaxWidth().padding(top = 10.dp),
            ) { Text("Save settings") }
        }
    }
}
