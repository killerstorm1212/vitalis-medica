          repository.resetErrors()
            AnalysisScheduler.scheduleAnalysisIfNeeded(getApplication())
        }
    }

    fun refreshCrashReport() { _lastCrash.value = readLastCrash(getApplication()) }

    companion object {
        private fun readLastCrash(application: Application): String? {
            return try {
                val f = File(application.filesDir, "last_crash.txt")
                if (f.exists()) f.readText().take(6000) else null
            } catch (_: Exception) { null }
        }
    }
}
''')

# 6) Library screen: page from DB instead of filtering a 10k in-memory list.
p = root/'app/src/main/java/com/mooddna/ui/screens/LibraryScreen.kt'
p.write_text('''package com.mooddna.ui.screens

import androidx.compose.foundation.background
import androidx.compose.foundation.layout.*
import androidx.compose.foundation.lazy.LazyColumn
import androidx.compose.foundation.lazy.items
import androidx.compose.material.icons.Icons
import androidx.compose.material.icons.filled.Search
import androidx.compose.material3.*
import androidx.compose.runtime.*
import androidx.compose.ui.Alignment
import androidx.compose.ui.Modifier
import androidx.compose.ui.unit.dp
import com.mooddna.ui.components.TrackItem
import com.mooddna.ui.viewmodel.MainViewModel

@Composable
fun LibraryScreen(viewModel: MainViewModel, modifier: Modifier = Modifier) {
    val tracks by viewModel.libraryTracks.collectAsState()
    val loading by viewModel.libraryLoading.collectAsState()
    val hasMore by viewModel.libraryHasMore.collectAsState()
    val totalCount by viewModel.totalCount.collectAsState()
    var searchQuery by remember { mutableStateOf("") }

    // Refresh the first page as MediaStore batches arrive. Analysis updates do not change totalCount.
    LaunchedEffect(totalCount) { viewModel.reloadLibrary() }

    Column(
        modifier = modifier.fillMaxSize().background(MaterialTheme.colorScheme.background).padding(16.dp)
    ) {
        Text("Library", style = MaterialTheme.typography.headlineLarge)
        Spacer(Modifier.height(12.dp))
        OutlinedTextField(
            value = searchQuery,
            onValueChange = { searchQuery = it; viewModel.setLibrarySearch(it) },
            placeholder = { Text("Search songs, singers, films, composers...") },
            leadingIcon = { Icon(Icons.Default.Search, contentDescription = null) },
            modifier = Modifier.fillMaxWidth(),
            shape = MaterialTheme.shapes.medium,
            singleLine = true
        )
        Spacer(Modifier.height(12.dp))
        Text(
            if (searchQuery.isBlank()) "$totalCount tracks • showing ${tracks.size}" else "${tracks.size} matching tracks loaded",
            style = MaterialTheme.typography.titleMedium
        )
        Spacer(Modifier.height(8.dp))

        LazyColumn(
            modifier = Modifier.fillMaxSize(),
            verticalArrangement = Arrangement.spacedBy(10.dp)
        ) {
            items(tracks, key = { it.id }) { track ->
                TrackItem(
                    track = track,
                    onClick = { viewModel.playTrack(track, tracks) },
                    onRadioClick = { viewModel.startMoodRadio(track) }
                )
            }

            if (loading) {
                item {
                    Box(Modifier.fillMaxWidth().padding(20.dp), contentAlignment = Alignment.Center) {
                        CircularProgressIndicator()
                    }
                }
            } else if (hasMore) {
                item {
                    OutlinedButton(
                        onClick = { viewModel.loadMoreLibrary() },
                        modifier = Modifier.fillMaxWidth().padding(vertical = 8.dp)
                    ) { Text("Load 200 more") }
                }
            }
        }
    }
}
''')

# 7) Scheduler: scanning and analysis are separate unique work; analysis is short chained chunks.
p = root/'app/src/main/java/com/mooddna/engine/pipeline/AnalysisScheduler.kt'
p.write_text('''package com.mooddna.engine.pipeline

import android.content.Context
import androidx.work.*
import java.util.concurrent.TimeUnit

object AnalysisScheduler {
    const val WORK_TAG_SCAN = "mooddna_work_scan"
    const val WORK_TAG_ANALYSIS = "mooddna_work_analysis"
    private const val ANALYSIS_UNIQUE = "MoodDnaAnalysisLoop"

    private fun constraints(chargingOnly: Boolean = false) = Constraints.Builder()
        .setRequiresStorageNotLow(true)
        .setRequiresBatteryNotLow(true)
        .apply { if (chargingOnly) setRequiresCharging(true) }
        .build()

    fun scheduleFullPipeline(context: Context, chargingOnly: Boolean = false) {
        val scanRequest = OneTimeWorkRequestBuilder<LibraryScanWorker>()
            .setConstraints(constraints(chargingOnly))
            .addTag(WORK_TAG_SCAN)
            .build()

        WorkManager.getInstance(context).enqueueUniqueWork(
            "MoodDnaLibraryScan",
            ExistingWorkPolicy.KEEP,
            scanRequest
        )
    }

    fun scheduleAnalysisIfNeeded(context: Context) {
        val request = OneTimeWorkRequestBuilder<AudioAnalysisWorker>()
            .setConstraints(constraints())
            .setInitialDelay(3, TimeUnit.SECONDS)
            .addTag(WORK_TAG_ANALYSIS)
            .build()
        WorkManager.getInstance(context).enqueueUniqueWork(ANALYSIS_UNIQUE, ExistingWorkPolicy.KEEP, request)
    }

    fun scheduleAnalysisContinuation(context: Context) {
        val request = OneTimeWorkRequestBuilder<AudioAnalysisWorker>()
            .setConstraints(constraints())
            .setInitialDelay(1, TimeUnit.SECONDS)
            .addTag(WORK_TAG_ANALYSIS)
            .build()
        WorkManager.getInstance(context).enqueueUniqueWork(ANALYSIS_UNIQUE, ExistingWorkPolicy.APPEND_OR_REPLACE, request)
    }
}
''')

# 8) Scan worker starts analysis once scan is complete.
p = root/'app/src/main/java/com/mooddna/engine/pipeline/LibraryScanWorker.kt'
p.write_text('''package com.mooddna.engine.pipeline

import android.content.Context
import androidx.work.CoroutineWorker
import androidx.work.WorkerParameters
import com.mooddna.MoodDnaApp

class LibraryScanWorker(appContext: Context, params: WorkerParameters) : CoroutineWorker(appContext, params) {
    override suspend fun doWork(): Result {
        val app = applicationContext as MoodDnaApp
        return try {
            app.repository.scanMediaStore()
            AnalysisScheduler.scheduleAnalysisIfNeeded(applicationContext)
            Result.success()
        } catch (_: Exception) {
            Result.retry()
        }
    }
}
''')

# 9) Audio worker: short bounded chunks suitable for 10k libraries.
p = root/'app/src/main/java/com/mooddna/engine/pipeline/AudioAnalysisWorker.kt'
s = p.read_text()
s = s.replace('''        val totalPending = database.openHelper.readableDatabase.let {''', '''        // Recover a track left in AUDIO_ANALYSING if Android killed a previous worker.
        trackDao.resetStuckAnalyzing()

        val totalPending = database.openHelper.readableDatabase.let {''')
s = s.replace('var processed = 0\n        val batchLimit = 20', 'var processed = 0\n        val batchLimit = 2\n        val maxTracksThisRun = 40')
s = s.replace('while (true) {', 'while (processed < maxTracksThisRun) {', 1)
s = s.replace('maxDurationSecs = 20', 'maxDurationSecs = 8')
s = s.replace('''                processed++
                if (processed % 5 == 0) {''', '''                processed++
                kotlinx.coroutines.yield()
                if (processed % 5 == 0) {''')
s = s.replace('''        (applicationContext as MoodDnaApp).repository.refreshVectorIndex()
        Result.success()
''', '''        val remaining = trackDao.getPendingTrackCountOnce()
        if (remaining > 0 && !isStopped) {
            AnalysisScheduler.scheduleAnalysisContinuation(applicationContext)
        } else if (remaining == 0) {
            (applicationContext as MoodDnaApp).repository.refreshVectorIndex()
        }
        Result.success()
''')
assert 'maxTracksThisRun = 40' in s and 'maxDurationSecs = 8' in s and 'scheduleAnalysisContinuation' in s
p.write_text(s)

# 10) Crash recorder for persistent diagnostics after process death.
p = root/'app/src/main/java/com/mooddna/MoodDnaApp.kt'
s = p.read_text()
s = s.replace('import com.mooddna.data.repository.MusicRepository', 'import com.mooddna.data.repository.MusicRepository\nimport java.io.File\nimport java.io.PrintWriter\nimport java.io.StringWriter')
s = s.replace('''        instance = this
        database = MoodDatabase.getInstance(this)''', '''        instance = this
        installCrashRecorder()
        database = MoodDatabase.getInstance(this)''')
marker = '''    private fun createNotificationChannels() {'''
method = '''    private fun installCrashRecorder() {
        val previous = Thread.getDefaultUncaughtExceptionHandler()
        Thread.setDefaultUncaughtExceptionHandler { thread, throwable ->
            try {
                val sw = StringWriter()
                throwable.printStackTrace(PrintWriter(sw))
                File(filesDir, "last_crash.txt").writeText(
                    "Thread: ${thread.name}\\nTime: ${System.currentTimeMillis()}\\n${sw}"
                )
            } catch (_: Throwable) { }
            previous?.uncaughtException(thread, throwable)
        }
    }

'''
assert marker in s
s = s.replace(marker, method + marker)
p.write_text(s)

# 11) Diagnostics displays persisted last crash if one exists.
p = root/'app/src/main/java/com/mooddna/ui/screens/DiagnosticsScreen.kt'
s = p.read_text()
s = s.replace('''    val failedCount by viewModel.failedCount.collectAsState()
''', '''    val failedCount by viewModel.failedCount.collectAsState()
    val lastCrash by viewModel.lastCrash.collectAsState()
''')
needle = '''        OutlinedButton(
            onClick = { viewModel.retryFailedAnalysis() },
            modifier = Modifier.fillMaxWidth()
        ) {
            Text("Retry Failed Tracks")
        }
'''
insert = needle + '''

        lastCrash?.let { crash ->
            Text("Last crash report", style = MaterialTheme.typography.titleMedium, fontWeight = FontWeight.Bold)
            Card(modifier = Modifier.fillMaxWidth()) {
                Text(
                    text = crash,
                    style = MaterialTheme.typography.bodySmall,
                    modifier = Modifier.padding(12.dp),
                    maxLines = 12
                )
            }
        }
'''
assert needle in s
s=s.replace(needle,insert)
p.write_text(s)

# 12) Version
p = root/'app/build.gradle.kts'
s = p.read_text().replace('versionCode = 1', 'versionCode = 3').replace('versionName = "1.0.0"', 'versionName = "1.0.2-10k"')
p.write_text(s)

print('patched')
