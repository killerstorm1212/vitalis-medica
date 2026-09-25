from pathlib import Path

root = Path('/tmp/mooddna-src/MoodDNA')

# 10k+ SAFE ANALYSIS MODE:
# Keep the UI process quiet while the user is active. Heavy decode work only runs when
# the device is charging + idle, in small WorkManager attempts with poison-track quarantine.

# Make PCM windows tiny and strictly bounded.
p = root/'app/src/main/java/com/mooddna/engine/audio/AudioDecoder.kt'
s = p.read_text()
s = s.replace('800_000L', '200_000L')
p.write_text(s)

# Quarantine a track if the process died twice while it was AUDIO_ANALYSING.
p = root/'app/src/main/java/com/mooddna/data/local/dao/TrackDao.kt'
s = p.read_text()
old = '''    @Query("UPDATE tracks SET analysisState = 'AUDIO_PENDING' WHERE analysisState = 'AUDIO_ANALYSING'")
    suspend fun resetStuckAnalyzing()
'''
new = '''    @Query("""
        UPDATE tracks
        SET analysisState = CASE
                WHEN retryCount >= 2 THEN 'FAILED_PERMANENT'
                ELSE 'AUDIO_PENDING'
            END,
            lastError = CASE
                WHEN retryCount >= 2 THEN 'Analyzer process stopped unexpectedly; track quarantined'
                ELSE lastError
            END
        WHERE analysisState = 'AUDIO_ANALYSING'
    """)
    suspend fun resetStuckAnalyzing()
'''
assert old in s
s = s.replace(old, new)
p.write_text(s)

# Scheduler: automatic analysis runs only while charging + idle. This keeps a 10k scan
# from immediately launching thousands of decodes while the user is interacting with the app.
p = root/'app/src/main/java/com/mooddna/engine/pipeline/AnalysisScheduler.kt'
p.write_text('''package com.mooddna.engine.pipeline

import android.content.Context
import androidx.work.*
import java.util.concurrent.TimeUnit

object AnalysisScheduler {
    const val WORK_TAG_SCAN = "mooddna_work_scan"
    const val WORK_TAG_ANALYSIS = "mooddna_work_analysis"
    private const val ANALYSIS_UNIQUE = "MoodDnaSafeAnalysis"

    private fun scanConstraints() = Constraints.Builder()
        .setRequiresStorageNotLow(true)
        .setRequiresBatteryNotLow(true)
        .build()

    private fun safeAnalysisConstraints() = Constraints.Builder()
        .setRequiresStorageNotLow(true)
        .setRequiresBatteryNotLow(true)
        .setRequiresCharging(true)
        .setRequiresDeviceIdle(true)
        .build()

    fun scheduleFullPipeline(context: Context, chargingOnly: Boolean = false) {
        val scanRequest = OneTimeWorkRequestBuilder<LibraryScanWorker>()
            .setConstraints(scanConstraints())
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
            .setConstraints(safeAnalysisConstraints())
            .setInitialDelay(30, TimeUnit.SECONDS)
            .setBackoffCriteria(BackoffPolicy.LINEAR, 10, TimeUnit.SECONDS)
            .addTag(WORK_TAG_ANALYSIS)
            .build()

        WorkManager.getInstance(context).enqueueUniqueWork(
            ANALYSIS_UNIQUE,
            ExistingWorkPolicy.KEEP,
            request
        )
    }

    // Kept for compatibility with older ViewModel calls.
    fun scheduleAnalysisContinuation(context: Context) = scheduleAnalysisIfNeeded(context)
}
''')

# Worker: no foreground-service promotion, only five songs per WorkManager attempt.
# Result.retry() reuses the same work request rather than constructing a 10,000-node chain.
p = root/'app/src/main/java/com/mooddna/engine/pipeline/AudioAnalysisWorker.kt'
p.write_text('''package com.mooddna.engine.pipeline

import android.content.Context
import android.net.Uri
import androidx.work.CoroutineWorker
import androidx.work.WorkerParameters
import com.mooddna.MoodDnaApp
import com.mooddna.data.local.entity.AnalysisState
import com.mooddna.engine.audio.AudioDecoder
import com.mooddna.engine.audio.DspFeatureExtractor
import com.mooddna.engine.mood.MoodVectorCalculator
import kotlinx.coroutines.CancellationException
import kotlinx.coroutines.Dispatchers
import kotlinx.coroutines.delay
import kotlinx.coroutines.withContext
import kotlinx.coroutines.yield

class AudioAnalysisWorker(
    appContext: Context,
    params: WorkerParameters
) : CoroutineWorker(appContext, params) {

    private val app = appContext as MoodDnaApp
    private val trackDao = app.database.trackDao()

    override suspend fun doWork(): Result = withContext(Dispatchers.IO) {
        // If Android/native codec killed a prior attempt, release or quarantine that track.
        trackDao.resetStuckAnalyzing()

        var processed = 0
        val maxTracksThisAttempt = 5

        while (processed < maxTracksThisAttempt && !isStopped) {
            val track = trackDao.getPendingTracks(1).firstOrNull() ?: break
            val attempt = track.retryCount + 1

            try {
                // Persist the attempt BEFORE touching MediaCodec. If the process dies natively,
                // next launch can identify the poison track and eventually quarantine it.
                trackDao.update(
                    track.copy(
                        analysisState = AnalysisState.AUDIO_ANALYSING,
                        retryCount = attempt,
                        lastError = null
                    )
                )

                val pcm = AudioDecoder.decodeSampledAudio(
                    applicationContext,
                    Uri.parse(track.contentUri),
                    maxDurationSecs = 2
                )

                val dsp = DspFeatureExtractor.extract(pcm)
                val moodResult = MoodVectorCalculator.calculate(
                    features = dsp,
                    genre = track.genre,
                    film = track.filmOrSoundtrack,
                    artist = track.artist,
                    title = track.title,
                    year = track.year
                )
                val mv = moodResult.vector

                trackDao.update(
                    track.copy(
                        bpm = dsp.bpm,
                        spectralCentroid = dsp.spectralCentroid,
                        rmsEnergy = dsp.rmsEnergy,
                        zeroCrossingRate = dsp.zeroCrossingRate,
                        valence = mv.valence,
                        energy = mv.energy,
                        tension = mv.tension,
                        tempoFeel = mv.tempoFeel,
                        danceability = mv.danceability,
                        acousticCharacter = mv.acousticCharacter,
                        intensity = mv.intensity,
                        romance = mv.romance,
                        melancholy = mv.melancholy,
                        uplift = mv.uplift,
                        aggression = mv.aggression,
                        dreaminess = mv.dreaminess,
                        groove = mv.groove,
                        cinematicCharacter = mv.cinematicCharacter,
                        vocalCharacter = mv.vocalCharacter,
                        complexity = mv.complexity,
                        confidenceScore = moodResult.confidence,
                        analysisState = AnalysisState.MOOD_COMPLETE,
                        retryCount = attempt,
                        lastError = null
                    )
                )
            } catch (cancel: CancellationException) {
                throw cancel
            } catch (t: Throwable) {
                val permanent = attempt >= 3
                try {
                    trackDao.update(
                        track.copy(
                            analysisState = if (permanent) AnalysisState.FAILED_PERMANENT else AnalysisState.FAILED_RETRYABLE,
                            retryCount = attempt,
                            lastError = (t.javaClass.simpleName + ": " + (t.message ?: "analysis error")).take(800)
                        )
                    )
                } catch (_: Throwable) { }
            }

            processed++
            yield()
            delay(150)
            if (processed % 2 == 0) System.gc()
        }

        val remaining = trackDao.getPendingTrackCountOnce()
        if (remaining > 0 && !isStopped) {
            // Re-run later under the same charging+idle constraints.
            Result.retry()
        } else {
            try { app.repository.refreshVectorIndex() } catch (_: Throwable) { }
            Result.success()
        }
    }
}
''')

# v1.0.3
p = root/'app/build.gradle.kts'
s = p.read_text()
s = s.replace('versionCode = 3', 'versionCode = 4')
s = s.replace('versionName = "1.0.2-10k"', 'versionName = "1.0.3-10k-safe"')
p.write_text(s)

print("10k safe-analysis patch applied")
