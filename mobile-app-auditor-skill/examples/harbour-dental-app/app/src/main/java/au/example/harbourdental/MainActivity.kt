package au.example.harbourdental

import android.content.Intent
import android.os.Bundle
import androidx.activity.ComponentActivity
import androidx.activity.compose.setContent
import androidx.compose.foundation.Canvas
import androidx.compose.foundation.background
import androidx.compose.foundation.clickable
import androidx.compose.foundation.layout.Arrangement
import androidx.compose.foundation.layout.Box
import androidx.compose.foundation.layout.Column
import androidx.compose.foundation.layout.Row
import androidx.compose.foundation.layout.Spacer
import androidx.compose.foundation.layout.fillMaxSize
import androidx.compose.foundation.layout.fillMaxWidth
import androidx.compose.foundation.layout.height
import androidx.compose.foundation.layout.padding
import androidx.compose.foundation.layout.size
import androidx.compose.material.icons.Icons
import androidx.compose.material.icons.filled.Add
import androidx.compose.material.icons.filled.KeyboardArrowRight
import androidx.compose.material3.FloatingActionButton
import androidx.compose.material3.Icon
import androidx.compose.material3.Text
import androidx.compose.runtime.Composable
import androidx.compose.runtime.LaunchedEffect
import androidx.compose.runtime.getValue
import androidx.compose.runtime.mutableIntStateOf
import androidx.compose.runtime.remember
import androidx.compose.runtime.setValue
import androidx.compose.ui.Alignment
import androidx.compose.ui.Modifier
import androidx.compose.ui.graphics.Color
import androidx.compose.ui.platform.testTag
import androidx.compose.ui.semantics.heading
import androidx.compose.ui.semantics.semantics
import androidx.compose.ui.text.font.FontWeight
import androidx.compose.ui.unit.dp
import androidx.compose.ui.unit.sp
import kotlinx.coroutines.delay

class MainActivity : ComponentActivity() {
    override fun onCreate(savedInstanceState: Bundle?) {
        super.onCreate(savedInstanceState)
        setContent {
            HarbourTheme {
                HomeScreen(
                    onBook = { startActivity(Intent(this, BookingActivity::class.java)) },
                    onMap = { startActivity(Intent(this, MapActivity::class.java)) },
                )
            }
        }
    }
}

private val promos = listOf("Free check-up for new patients", "Late appointments on Thursdays", "Gentle care for nervous patients")

@Composable
fun HomeScreen(onBook: () -> Unit, onMap: () -> Unit) {
    // Planted: the promotion banner advances every 3 seconds and has no pause control (2.2.2).
    var promo by remember { mutableIntStateOf(0) }
    LaunchedEffect(Unit) {
        while (true) {
            delay(3000)
            promo = (promo + 1) % promos.size
        }
    }
    Box(TagsAsIds.fillMaxSize().background(Color.White)) {
        Column(Modifier.fillMaxSize()) {
            Box(Modifier.fillMaxWidth().background(Teal).padding(16.dp)) {
                Text("Harbour Dental", color = Color.White, fontSize = 22.sp, fontWeight = FontWeight.Bold,
                    modifier = Modifier.semantics { heading() }.testTag("title"))
            }
            // Decoy: a decorative illustration, correctly left out of the accessibility tree.
            Canvas(Modifier.fillMaxWidth().height(96.dp).testTag("hero_art")) {
                drawCircle(Color(0xFFE0F2F1), radius = size.minDimension / 2.2f)
                drawCircle(Color(0xFF80CBC4), radius = size.minDimension / 4f)
            }
            Box(Modifier.fillMaxWidth().background(Color(0xFFFFF4D6)).padding(16.dp).testTag("promo_banner")) {
                Text(promos[promo], fontSize = 16.sp, color = Color(0xFF3B2F00))
            }
            Text("Our clinics", fontSize = 18.sp, fontWeight = FontWeight.Bold,
                modifier = Modifier.padding(start = 16.dp, top = 16.dp).semantics { heading() })
            ClinicRow("Harbour Street", "Mon to Fri, 8am to 6pm", onMap, "clinic_harbour")
            ClinicRow("Northside", "Tue to Sat, 9am to 5pm", onMap, "clinic_northside")
            // Planted: grey hint text on white, about 2.3:1 (1.4.3).
            Text("Bookings open six weeks ahead", fontSize = 13.sp, color = Color(0xFFAAAAAA),
                modifier = Modifier.padding(16.dp).testTag("booking_hint"))
        }
        // Planted: an icon-only Book button with no label (1.1.1, 4.1.2).
        FloatingActionButton(onClick = onBook, containerColor = Teal,
            modifier = Modifier.align(Alignment.BottomEnd).padding(24.dp).testTag("book_fab")) {
            Icon(Icons.Filled.Add, contentDescription = null, tint = Color.White)
        }
    }
}

@Composable
fun ClinicRow(name: String, hours: String, onClick: () -> Unit, tag: String) {
    // Decoy: the chevron is small, but the whole row is the touch target.
    Row(Modifier.fillMaxWidth().clickable(onClick = onClick).padding(horizontal = 16.dp, vertical = 14.dp).testTag(tag),
        verticalAlignment = Alignment.CenterVertically, horizontalArrangement = Arrangement.SpaceBetween) {
        Column {
            Text(name, fontSize = 16.sp, color = Color(0xFF1A1A1A))
            Text(hours, fontSize = 14.sp, color = Color(0xFF555555))
        }
        Spacer(Modifier.size(8.dp))
        Icon(Icons.Filled.KeyboardArrowRight, contentDescription = null, modifier = Modifier.size(16.dp), tint = Color(0xFF555555))
    }
}
