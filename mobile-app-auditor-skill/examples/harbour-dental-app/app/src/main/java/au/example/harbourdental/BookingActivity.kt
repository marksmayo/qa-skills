package au.example.harbourdental

import android.os.Bundle
import androidx.activity.ComponentActivity
import androidx.activity.compose.setContent
import androidx.compose.foundation.background
import androidx.compose.foundation.border
import androidx.compose.foundation.clickable
import androidx.compose.foundation.focusable
import androidx.compose.foundation.layout.Box
import androidx.compose.foundation.layout.Column
import androidx.compose.foundation.layout.Row
import androidx.compose.foundation.layout.fillMaxSize
import androidx.compose.foundation.layout.fillMaxWidth
import androidx.compose.foundation.layout.height
import androidx.compose.foundation.layout.padding
import androidx.compose.foundation.layout.size
import androidx.compose.foundation.layout.Spacer
import androidx.compose.foundation.layout.width
import androidx.compose.material.icons.Icons
import androidx.compose.material.icons.filled.Close
import androidx.compose.material.icons.filled.Info
import androidx.compose.material3.Button
import androidx.compose.material3.Icon
import androidx.compose.material3.Text
import androidx.compose.runtime.Composable
import androidx.compose.runtime.LaunchedEffect
import androidx.compose.runtime.getValue
import androidx.compose.runtime.mutableStateOf
import androidx.compose.runtime.remember
import androidx.compose.runtime.saveable.rememberSaveable
import androidx.compose.runtime.setValue
import androidx.compose.ui.Modifier
import androidx.compose.ui.focus.FocusRequester
import androidx.compose.ui.focus.focusRequester
import androidx.compose.ui.graphics.Color
import androidx.compose.ui.input.key.Key
import androidx.compose.ui.input.key.key
import androidx.compose.ui.input.key.onPreviewKeyEvent
import androidx.compose.ui.platform.testTag
import androidx.compose.ui.semantics.contentDescription
import androidx.compose.ui.semantics.semantics
import androidx.compose.ui.unit.dp
import androidx.compose.ui.unit.sp
import androidx.compose.ui.window.Dialog
import androidx.compose.ui.window.DialogProperties

class BookingActivity : ComponentActivity() {
    private var pickerOpen = false

    override fun onCreate(savedInstanceState: Bundle?) {
        super.onCreate(savedInstanceState)
        // Planted: when the activity is recreated (a rotation) with the picker open, it expects a draft day
        // that was never saved, and crashes (B2).
        if (savedInstanceState?.getBoolean("picker_open") == true) {
            val draft = savedInstanceState.getString("draft_day")!!
            title = draft
        }
        setContent { HarbourTheme { BookingScreen(onClose = { finish() }, onPicker = { pickerOpen = it }) } }
    }

    override fun onSaveInstanceState(outState: Bundle) {
        super.onSaveInstanceState(outState)
        outState.putBoolean("picker_open", pickerOpen)
    }
}

@Composable
fun BookingScreen(onClose: () -> Unit, onPicker: (Boolean) -> Unit) {
    var pickerOpen by rememberSaveable { mutableStateOf(false) }
    var chosen by remember { mutableStateOf("No date chosen") }
    Column(TagsAsIds.fillMaxSize().background(Color.White).padding(16.dp)) {
        Row(Modifier.fillMaxWidth()) {
            Text("Book a visit", fontSize = 22.sp, modifier = Modifier.weight(1f))
            // Planted: 20 dp Help and Close buttons 4 dp apart (2.5.8 needs 24 x 24, or 24 dp spacing).
            Box(Modifier.size(20.dp).clickable(onClick = {}).semantics { contentDescription = "Help" }.testTag("help_button")) {
                Icon(Icons.Filled.Info, contentDescription = null, modifier = Modifier.size(20.dp))
            }
            Spacer(Modifier.width(4.dp))
            Box(Modifier.size(20.dp).clickable(onClick = onClose).semantics { contentDescription = "Close" }.testTag("close_button")) {
                Icon(Icons.Filled.Close, contentDescription = null, modifier = Modifier.size(20.dp))
            }
        }
        // Planted: an explanation in a fixed-height box, so larger text is cut off (1.4.4).
        Box(Modifier.fillMaxWidth().height(40.dp).padding(top = 8.dp).testTag("intro_box")) {
            Text("Choose a day and we will call you to confirm the time.", fontSize = 15.sp, maxLines = 1,
                modifier = Modifier.testTag("intro_text"))
        }
        Text(chosen, fontSize = 16.sp, modifier = Modifier.padding(vertical = 16.dp).testTag("chosen_date"))
        Button(onClick = { pickerOpen = true; onPicker(true) }, modifier = Modifier.testTag("pick_date")) { Text("Pick a date") }
    }
    if (pickerOpen) {
        DatePickerDialog(onPick = { chosen = it; pickerOpen = false; onPicker(false) })
    }
}

@Composable
fun DatePickerDialog(onPick: (String) -> Unit) {
    val first = remember { FocusRequester() }
    // Planted: the dialog swallows Tab, Escape and Back, so a keyboard user can't move or leave (2.1.2).
    Dialog(onDismissRequest = {}, properties = DialogProperties(dismissOnBackPress = false, dismissOnClickOutside = false)) {
        Column(TagsAsIds.background(Color.White).padding(16.dp).testTag("date_dialog")
            .onPreviewKeyEvent { it.key == Key.Tab || it.key == Key.Escape || it.key == Key.Back || it.key == Key.DirectionDown }) {
            Text("Pick a day", fontSize = 18.sp)
            listOf("Mon 16 Nov", "Tue 17 Nov", "Wed 18 Nov").forEachIndexed { i, d ->
                val mod = if (i == 0) Modifier.focusRequester(first) else Modifier
                Text(d, fontSize = 16.sp, modifier = mod.fillMaxWidth().focusable().border(1.dp, Color(0xFFDDDDDD))
                    .clickable { onPick(d) }.padding(12.dp).testTag("day_$i"))
            }
        }
    }
    LaunchedEffect(Unit) { first.requestFocus() }
}
