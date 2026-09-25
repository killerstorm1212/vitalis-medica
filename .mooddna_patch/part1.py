from pathlib import Path
import re

root = Path('/tmp/mooddna-src/MoodDNA')

# 1) Launcher vector fix
p = root/'app/src/main/res/drawable/ic_launcher_foreground.xml'
p.write_text('''<?xml version="1.0" encoding="utf-8"?>
<vector xmlns:android="http://schemas.android.com/apk/res/android"
    android:width="108dp"
    android:height="108dp"
    android:viewportWidth="108"
    android:viewportHeight="108">
    <path
        android:fillColor="#FFFFFF"
        android:pathData="M54,20 C35.2,20 20,35.2 20,54 C20,72.8 35.2,88 54,88 C72.8,88 88,72.8 88,54 C88,35.2 72.8,20 54,20 Z M54,78 C40.8,78 30,67.2 30,54 C30,40.8 40.8,30 54,30 C67.2,30 78,40.8 78,54 C78,67.2 67.2,78 54,78 Z"/>
    <path
        android:fillColor="#E3DFFF"
        android:pathData="M54,42 C47.373,42 42,47.373 42,54 C42,60.627 47.373,66 54,66 C60.627,66 66,60.627 66,54 C66,47.373 60.627,42 54,42 Z"/>
</vector>
''')

# 2) Audio decoder memory hardening
p = root/'app/src/main/java/com/mooddna/engine/audio/AudioDecoder.kt'
s = p.read_text()
s = s.replace(
    'val maxSamples = sampleRate * channelCount * maxDurationSecs\n            val pcmFloats = ArrayList<Float>(maxSamples)',
    'val maxSamples = minOf(sampleRate.toLong() * channelCount.toLong() * maxDurationSecs.toLong(), 800_000L).toInt()\n            val pcmFloats = FloatArray(maxSamples)'
)
s = s.replace('pcmFloats.add(sample)\n                            samplesCollected++', 'pcmFloats[samplesCollected] = sample\n                            samplesCollected++')
s = s.replace('samples = pcmFloats.toFloatArray(),', 'samples = pcmFloats.copyOf(samplesCollected),')
assert 'FloatArray(maxSamples)' in s and '800_000L' in s and 'copyOf(samplesCollected)' in s
p.write_text(s)

# 3) DAO: page library queries + efficient scan ids + stuck-analysis recovery
p = root/'app/src/main/java/com/mooddna/data/local/dao/TrackDao.kt'
s = p.read_text()
needle = '''    @Query("SELECT * FROM tracks ORDER BY dateAdded DESC")
    fun getAllTracksFlow(): Flow<List<TrackEntity>>
'''
insert = needle + '''
    @Query("SELECT * FROM tracks ORDER BY dateAdded DESC LIMIT :limit")
    fun getRecentTracksFlow(limit: Int): Flow<List<TrackEntity>>

    @Query("SELECT * FROM tracks ORDER BY dateAdded DESC LIMIT :limit OFFSET :offset")
    suspend fun getTracksPage(limit: Int, offset: Int): List<TrackEntity>

    @Query("""
        SELECT * FROM tracks
        WHERE title LIKE '%' || :query || '%'
           OR artist LIKE '%' || :query || '%'
           OR album LIKE '%' || :query || '%'
           OR filmOrSoundtrack LIKE '%' || :query || '%'
           OR composer LIKE '%' || :query || '%'
           OR lyricist LIKE '%' || :query || '%'
           OR genre LIKE '%' || :query || '%'
        ORDER BY title ASC
        LIMIT :limit OFFSET :offset
    """)
    suspend fun searchTracksPage(query: String, limit: Int, offset: Int): List<TrackEntity>

    @Query("SELECT mediaStoreId FROM tracks")
    suspend fun getAllMediaStoreIds(): List<Long>

    @Query("UPDATE tracks SET analysisState = 'AUDIO_PENDING' WHERE analysisState = 'AUDIO_ANALYSING'")
    suspend fun resetStuckAnalyzing()

    @Query("SELECT COUNT(*) FROM tracks WHERE analysisState != 'MOOD_COMPLETE' AND analysisState != 'FAILED_PERMANENT' AND analysisState != 'AUDIO_ANALYSING'")
    suspend fun getPendingTrackCountOnce(): Int
'''
assert needle in s
s = s.replace(needle, insert)
# Exclude currently claimed track from other workers.
s = s.replace(
    '@Query("SELECT * FROM tracks WHERE analysisState != \'MOOD_COMPLETE\' AND analysisState != \'FAILED_PERMANENT\' ORDER BY id ASC LIMIT :limit")',
    '@Query("SELECT * FROM tracks WHERE analysisState != \'MOOD_COMPLETE\' AND analysisState != \'FAILED_PERMANENT\' AND analysisState != \'AUDIO_ANALYSING\' ORDER BY id ASC LIMIT :limit")'
)
p.write_text(s)

# 4) Repository: never stream 10k full entities to Compose; page library from DB; avoid N+1 scan queries
p = root/'app/src/main/java/com/mooddna/data/repository/MusicRepository.kt'
s = p.read_text()
s = s.replace('val allTracksFlow: Flow<List<TrackEntity>> = trackDao.getAllTracksFlow()', 'val allTracksFlow: Flow<List<TrackEntity>> = trackDao.getRecentTracksFlow(50)')
needle = '''    suspend fun generateJourneyQueue(journey: MoodJourney, steps: Int = 15): List<TrackEntity> {
        return withContext(Dispatchers.IO) {
            vectorIndex.generateJourney(journey.startMood, journey.endMood, steps)
        }
    }
'''
insert = needle + '''
    suspend fun loadLibraryPage(query: String, limit: Int = 200, offset: Int = 0): List<TrackEntity> {
        return withContext(Dispatchers.IO) {
            if (query.isBlank()) trackDao.getTracksPage(limit, offset)
            else trackDao.searchTracksPage(query.trim(), limit, offset)
        }
    }
'''
assert needle in s
s = s.replace(needle, insert)
s = s.replace('''        val tracksToInsert = ArrayList<TrackEntity>()
        var count = 0
''', '''        val tracksToInsert = ArrayList<TrackEntity>(500)
        val knownMediaIds = trackDao.getAllMediaStoreIds().toHashSet()
        var count = 0
''')
s = s.replace('''                val mediaId = cursor.getLong(idCol)
                val existing = trackDao.getTrackByMediaStoreId(mediaId)
                if (existing != null) continue
''', '''                val mediaId = cursor.getLong(idCol)
                if (!knownMediaIds.add(mediaId)) continue
''')
s = s.replace('if (tracksToInsert.size >= 250)', 'if (tracksToInsert.size >= 500)')
p.write_text(s)

# 5) MainViewModel: DB-backed paged library. allTracks is now only recent 50.
p = root/'app/src/main/java/com/mooddna/ui/viewmodel/MainViewModel.kt'
p.write_text('''package com.mooddna.ui.viewmodel

import android.app.Application
import androidx.lifecycle.AndroidViewModel
import androidx.lifecycle.viewModelScope
import com.mooddna.MoodDnaApp
import com.mooddna.core.model.MoodJourney
import com.mooddna.core.model.MoodVector
import com.mooddna.data.local.entity.TrackEntity
import com.mooddna.engine.pipeline.AnalysisScheduler
import com.mooddna.service.PlaybackService
import kotlinx.coroutines.Job
import kotlinx.coroutines.delay
import kotlinx.coroutines.flow.*
import kotlinx.coroutines.launch
import java.io.File

class MainViewModel(application: Application) : AndroidViewModel(application) {

    private val repository = (application as MoodDnaApp).repository

    // Deliberately capped at 50 recent tracks. Never keep a 10k-track Room result in Compose state.
    val allTracks: StateFlow<List<TrackEntity>> = repository.allTracksFlow
        .stateIn(viewModelScope, SharingStarted.WhileSubscribed(5000), emptyList())

    val totalCount: StateFlow<Int> = repository.totalCountFlow
        .stateIn(viewModelScope, SharingStarted.WhileSubscribed(5000), 0)
    val analyzedCount: StateFlow<Int> = repository.analyzedCountFlow
        .stateIn(viewModelScope, SharingStarted.WhileSubscribed(5000), 0)
    val pendingCount: StateFlow<Int> = repository.pendingCountFlow
        .stateIn(viewModelScope, SharingStarted.WhileSubscribed(5000), 0)
    val failedCount: StateFlow<Int> = repository.failedCountFlow
        .stateIn(viewModelScope, SharingStarted.WhileSubscribed(5000), 0)

    val currentTrack: StateFlow<TrackEntity?> = PlaybackService.currentTrack
    val isPlaying: StateFlow<Boolean> = PlaybackService.isPlayingFlow

    private val _mixerMood = MutableStateFlow(
        MoodVector(
            valence = 0.5f, energy = 0.5f, intensity = 0.5f,
            uplift = 0.5f, dreaminess = 0.5f, romance = 0.5f
        )
    )
    val mixerMood = _mixerMood.asStateFlow()

    private val _mixerRankedTracks = MutableStateFlow<List<TrackEntity>>(emptyList())
    val mixerRankedTracks = _mixerRankedTracks.asStateFlow()

    private val _isMoodLocked = MutableStateFlow(false)
    val isMoodLocked = _isMoodLocked.asStateFlow()

    // Library paging state: 200 rows at a time, independent of the background analysis invalidations.
    private val pageSize = 200
    private var libraryOffset = 0
    private var libraryQuery = ""
    private var libraryLoadJob: Job? = null

    private val _libraryTracks = MutableStateFlow<List<TrackEntity>>(emptyList())
    val libraryTracks = _libraryTracks.asStateFlow()
    private val _libraryLoading = MutableStateFlow(false)
    val libraryLoading = _libraryLoading.asStateFlow()
    private val _libraryHasMore = MutableStateFlow(true)
    val libraryHasMore = _libraryHasMore.asStateFlow()

    private val _lastCrash = MutableStateFlow(readLastCrash(application))
    val lastCrash = _lastCrash.asStateFlow()

    init {
        viewModelScope.launch {
            repository.refreshVectorIndex()
            updateMixerRanking(_mixerMood.value)
        }
        reloadLibrary()
    }

    fun updateMixerSlider(updater: (MoodVector) -> MoodVector) {
        val newMood = updater(_mixerMood.value)
        _mixerMood.value = newMood
        updateMixerRanking(newMood)
    }

    private fun updateMixerRanking(targetMood: MoodVector) {
        viewModelScope.launch {
            _mixerRankedTracks.value = repository.getRankedByMood(targetMood, limit = 40)
        }
    }

    fun setLibrarySearch(query: String) {
        if (query == libraryQuery) return
        libraryQuery = query
        libraryLoadJob?.cancel()
        libraryLoadJob = viewModelScope.launch {
            delay(220)
            loadLibraryPage(reset = true)
        }
    }

    fun reloadLibrary() {
        libraryLoadJob?.cancel()
        libraryLoadJob = viewModelScope.launch { loadLibraryPage(reset = true) }
    }

    fun loadMoreLibrary() {
        if (_libraryLoading.value || !_libraryHasMore.value) return
        libraryLoadJob = viewModelScope.launch { loadLibraryPage(reset = false) }
    }

    private suspend fun loadLibraryPage(reset: Boolean) {
        if (_libraryLoading.value && !reset) return
        _libraryLoading.value = true
        try {
            if (reset) libraryOffset = 0
            val page = repository.loadLibraryPage(libraryQuery, pageSize, libraryOffset)
            _libraryTracks.value = if (reset) page else _libraryTracks.value + page
            libraryOffset += page.size
            _libraryHasMore.value = page.size == pageSize
        } finally {
            _libraryLoading.value = false
        }
    }

    fun playTrack(track: TrackEntity, queue: List<TrackEntity> = listOf(track)) {
        // Bound queue size so opening playback never materializes 10,000 MediaItems.
        val safeQueue = if (queue.size > 500) queue.take(500) else queue
        PlaybackService.playTrack(getApplication(), track, safeQueue)
    }

    fun startMoodRadio(seedTrack: TrackEntity) {
        viewModelScope.launch {
            val radioQueue = repository.getMoodRadio(seedTrack, limit = 30)
            val fullQueue = if (radioQueue.isNotEmpty()) listOf(seedTrack) + radioQueue else listOf(seedTrack)
            PlaybackService.playTrack(getApplication(), seedTrack, fullQueue)
        }
    }

    fun playJourney(journey: MoodJourney) {
        viewModelScope.launch {
            val journeyQueue = repository.generateJourneyQueue(journey, steps = 15)
            if (journeyQueue.isNotEmpty()) PlaybackService.playTrack(getApplication(), journeyQueue.first(), journeyQueue)
        }
    }

    fun toggleMoodLock() { _isMoodLocked.value = !_isMoodLocked.value }

    fun triggerLibraryScan() { AnalysisScheduler.scheduleFullPipeline(getApplication()) }

    fun retryFailedAnalysis() {
        viewModelScope.launch {
