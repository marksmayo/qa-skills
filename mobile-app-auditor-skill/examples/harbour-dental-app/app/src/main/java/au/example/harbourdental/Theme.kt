package au.example.harbourdental

import androidx.compose.material3.MaterialTheme
import androidx.compose.material3.lightColorScheme
import androidx.compose.runtime.Composable
import androidx.compose.ui.ExperimentalComposeUiApi
import androidx.compose.ui.Modifier
import androidx.compose.ui.graphics.Color
import androidx.compose.ui.semantics.semantics
import androidx.compose.ui.semantics.testTagsAsResourceId

val Teal = Color(0xFF0B5F6C)

/** Test tags become resource ids, so the auditor can name elements. */
@OptIn(ExperimentalComposeUiApi::class)
val TagsAsIds = Modifier.semantics { testTagsAsResourceId = true }

@Composable
fun HarbourTheme(content: @Composable () -> Unit) {
    MaterialTheme(colorScheme = lightColorScheme(primary = Teal, background = Color.White, surface = Color.White), content = content)
}
