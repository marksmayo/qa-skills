package au.example.harbourdental

import android.os.Bundle
import androidx.activity.ComponentActivity
import androidx.activity.compose.setContent
import androidx.compose.foundation.background
import androidx.compose.foundation.layout.Box
import androidx.compose.foundation.layout.fillMaxSize
import androidx.compose.material3.Text
import androidx.compose.ui.Alignment
import androidx.compose.ui.Modifier
import androidx.compose.ui.graphics.Color

class MapActivity : ComponentActivity() {
    override fun onCreate(savedInstanceState: Bundle?) {
        super.onCreate(savedInstanceState)
        // Planted: map tiles are "loaded" on the main thread, blocking it for 12 seconds (ANR).
        val tiles = loadTilesBlocking()
        setContent {
            HarbourTheme {
                Box(TagsAsIds.fillMaxSize().background(Color(0xFFE6F0F2)), contentAlignment = Alignment.Center) {
                    Text("Map: $tiles tiles")
                }
            }
        }
    }

    private fun loadTilesBlocking(): Int {
        Thread.sleep(12_000)
        return 24
    }
}
