package dev.danny.dailybrief

import androidx.compose.foundation.layout.Arrangement
import androidx.compose.foundation.layout.Column
import androidx.compose.foundation.layout.Row
import androidx.compose.foundation.layout.Spacer
import androidx.compose.foundation.layout.fillMaxSize
import androidx.compose.foundation.layout.fillMaxWidth
import androidx.compose.foundation.layout.height
import androidx.compose.foundation.layout.padding
import androidx.compose.foundation.layout.size
import androidx.compose.foundation.rememberScrollState
import androidx.compose.foundation.verticalScroll
import androidx.compose.material.icons.Icons
import androidx.compose.material.icons.filled.Delete
import androidx.compose.material3.Button
import androidx.compose.material3.Card
import androidx.compose.material3.CardDefaults
import androidx.compose.material3.CircularProgressIndicator
import androidx.compose.material3.HorizontalDivider
import androidx.compose.material3.Icon
import androidx.compose.material3.IconButton
import androidx.compose.material3.MaterialTheme
import androidx.compose.material3.OutlinedButton
import androidx.compose.material3.OutlinedTextField
import androidx.compose.material3.Text
import androidx.compose.runtime.Composable
import androidx.compose.runtime.LaunchedEffect
import androidx.compose.runtime.getValue
import androidx.compose.runtime.mutableStateOf
import androidx.compose.runtime.remember
import androidx.compose.runtime.rememberCoroutineScope
import androidx.compose.runtime.setValue
import androidx.compose.ui.Alignment
import androidx.compose.ui.Modifier
import androidx.compose.ui.platform.LocalContext
import androidx.compose.ui.text.font.FontFamily
import androidx.compose.ui.unit.dp
import kotlinx.coroutines.launch

@Composable
fun CalendarsScreen(modifier: Modifier = Modifier) {
    val context = LocalContext.current
    val scope = rememberCoroutineScope()

    var listing by remember { mutableStateOf(CalendarList()) }
    var loading by remember { mutableStateOf(true) }
    var label by remember { mutableStateOf("") }
    var url by remember { mutableStateOf("") }
    var error by remember { mutableStateOf<String?>(null) }
    var checking by remember { mutableStateOf(false) }
    var check by remember { mutableStateOf<CalendarCheck?>(null) }

    suspend fun reload() {
        listing = Calendars.list(context)
        loading = false
    }

    LaunchedEffect(Unit) { reload() }

    Column(
        modifier = modifier
            .fillMaxSize()
            .verticalScroll(rememberScrollState())
            .padding(16.dp),
        verticalArrangement = Arrangement.spacedBy(16.dp),
    ) {
        // Local val so Kotlin can smart cast it inside the branch below.
        val listError = listing.error
        Card(
            colors = if (listError != null) {
                CardDefaults.cardColors(containerColor = MaterialTheme.colorScheme.errorContainer)
            } else {
                CardDefaults.cardColors()
            },
        ) {
            Column(Modifier.padding(16.dp)) {
                Text("Connected calendars", style = MaterialTheme.typography.titleMedium)
                if (loading) {
                    CircularProgressIndicator(Modifier.size(20.dp).padding(top = 8.dp))
                } else if (listError != null) {
                    // A failed read is not an empty list. "None yet" here would
                    // invite re-pasting an address that is already on file, only
                    // to be told it is already connected. The delete rows and
                    // "Test connection" stay out of this branch: neither means
                    // anything against a list we could not read. "Add a
                    // calendar" below is still shown, so the user is not
                    // stranded on a screen with no controls.
                    Text(
                        "calendars.txt could not be read.",
                        style = MaterialTheme.typography.titleSmall,
                        modifier = Modifier.padding(top = 6.dp),
                    )
                    Text(
                        listError,
                        style = MaterialTheme.typography.bodySmall,
                        modifier = Modifier.padding(top = 6.dp),
                    )
                } else if (listing.entries.isEmpty()) {
                    Text(
                        "None yet. Today's events and the 3×3 session slot both need one.",
                        style = MaterialTheme.typography.bodySmall,
                        color = MaterialTheme.colorScheme.onSurfaceVariant,
                        modifier = Modifier.padding(top = 6.dp),
                    )
                } else {
                    listing.entries.forEachIndexed { index, entry ->
                        Row(
                            modifier = Modifier.fillMaxWidth().padding(top = 10.dp),
                            verticalAlignment = Alignment.CenterVertically,
                        ) {
                            Column(Modifier.weight(1f)) {
                                Text(
                                    entry.label.ifBlank { "Calendar ${index + 1}" },
                                    style = MaterialTheme.typography.bodyLarge,
                                )
                                // Never the address itself: it is a bearer token.
                                Text(
                                    entry.masked,
                                    style = MaterialTheme.typography.bodySmall,
                                    color = MaterialTheme.colorScheme.onSurfaceVariant,
                                    fontFamily = FontFamily.Monospace,
                                )
                            }
                            IconButton(onClick = {
                                scope.launch {
                                    Calendars.remove(context, index)
                                        .onFailure { error = it.message }
                                    check = null
                                    reload()
                                }
                            }) {
                                Icon(Icons.Default.Delete, contentDescription = "Remove")
                            }
                        }
                    }

                    OutlinedButton(
                        onClick = {
                            checking = true
                            error = null
                            scope.launch {
                                check = Calendars.check(context)
                                checking = false
                            }
                        },
                        enabled = !checking,
                        modifier = Modifier.fillMaxWidth().padding(top = 12.dp),
                    ) {
                        Text(if (checking) "Fetching…" else "Test connection")
                    }

                    check?.let { result ->
                        val text = when {
                            result.status == "ok" ->
                                "Working — ${result.events} event(s) today."
                            result.status == "empty" ->
                                "Working — nothing on today. ${result.reason}".trim()
                            else -> "Failed: ${result.reason}"
                        }
                        Text(
                            text,
                            style = MaterialTheme.typography.bodySmall,
                            color = if (result.ok) {
                                MaterialTheme.colorScheme.primary
                            } else {
                                MaterialTheme.colorScheme.error
                            },
                            modifier = Modifier.padding(top = 8.dp),
                        )
                    }
                }
            }
        }

        Card {
            Column(Modifier.padding(16.dp)) {
                Text("Add a calendar", style = MaterialTheme.typography.titleMedium)
                Text(
                    "In Google Calendar on the web: Settings → pick the calendar → " +
                        "\"Secret address in iCal format\". Any other iCal/ICS feed works too.",
                    style = MaterialTheme.typography.bodySmall,
                    color = MaterialTheme.colorScheme.onSurfaceVariant,
                    modifier = Modifier.padding(top = 6.dp),
                )
                OutlinedTextField(
                    value = label,
                    onValueChange = { label = it },
                    label = { Text("Name (optional)") },
                    singleLine = true,
                    modifier = Modifier.fillMaxWidth().padding(top = 10.dp),
                )
                OutlinedTextField(
                    value = url,
                    onValueChange = {
                        url = it
                        error = null
                    },
                    label = { Text("Secret iCal address") },
                    placeholder = { Text("https://calendar.google.com/calendar/ical/…/basic.ics") },
                    singleLine = true,
                    textStyle = MaterialTheme.typography.bodySmall.copy(fontFamily = FontFamily.Monospace),
                    modifier = Modifier.fillMaxWidth().padding(top = 8.dp),
                )
                Button(
                    onClick = {
                        scope.launch {
                            Calendars.add(context, label, url)
                                .onSuccess {
                                    label = ""
                                    url = ""
                                    check = null
                                    reload()
                                }
                                .onFailure { error = it.message }
                        }
                    },
                    enabled = url.isNotBlank(),
                    modifier = Modifier.fillMaxWidth().padding(top = 10.dp),
                ) { Text("Connect") }

                error?.let {
                    Text(
                        it,
                        style = MaterialTheme.typography.bodySmall,
                        color = MaterialTheme.colorScheme.error,
                        modifier = Modifier.padding(top = 8.dp),
                    )
                }
            }
        }

        Card(
            colors = CardDefaults.cardColors(
                containerColor = MaterialTheme.colorScheme.surfaceVariant,
            ),
        ) {
            Column(Modifier.padding(16.dp)) {
                Text("About that address", style = MaterialTheme.typography.titleSmall)
                HorizontalDivider(Modifier.padding(vertical = 8.dp))
                Text(
                    "A secret iCal address is a permanent password for that whole calendar — " +
                        "anyone holding it can read every event, and it keeps working until you " +
                        "reset it in Google Calendar.\n\n" +
                        "It is stored in calendars.txt in this app's private storage, is never " +
                        "shown again after you paste it, and is stripped out of logs and of the " +
                        "brief itself.",
                    style = MaterialTheme.typography.bodySmall,
                    color = MaterialTheme.colorScheme.onSurfaceVariant,
                )
            }
        }

        Spacer(Modifier.height(24.dp))
    }
}
