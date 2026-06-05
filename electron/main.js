const { app, BrowserWindow, ipcMain, Menu, dialog, shell } = require('electron');
const path = require('path');
const http = require('http');
const fs = require('fs');
const os = require('os');
const { spawn } = require('child_process');

// ── v4.76: userData rebrand from "nbme-self-assessment-suite" to "shamsulalamx" ──
// Electron's default app.getName() reads package.json's `name` field, which
// gives userData at ~/Library/Application Support/nbme-self-assessment-suite/.
// The .app filename is "shamsulalamx.app" (from build.productName), so the
// folder/.app naming mismatch confused the user.
//
// One-time atomic-rename migration: if the OLD folder exists and the NEW
// folder does NOT, rename old → new via fs.renameSync (atomic on same
// filesystem — no half-state). Then setPath() points Electron's userData
// at the new location. All tests / marks / scores / queue / Drive sync
// state / archive / IndexedDB preserved.
//
// MUST run before any code that calls app.getPath('userData'), which means
// at top-of-module before any other initialization. fs.renameSync is sync
// + fast (a few ms for a directory rename, regardless of size).
(function migrateUserDataFolderName() {
  try {
    const appDataRoot = app.getPath('appData');
    const OLD_NAME = 'nbme-self-assessment-suite';
    const NEW_NAME = 'shamsulalamx';
    const oldPath = path.join(appDataRoot, OLD_NAME);
    const newPath = path.join(appDataRoot, NEW_NAME);
    if (fs.existsSync(oldPath) && !fs.existsSync(newPath)) {
      fs.renameSync(oldPath, newPath);
      console.log('[userData migration] renamed', oldPath, '→', newPath);
    } else if (fs.existsSync(oldPath) && fs.existsSync(newPath)) {
      // Both exist — don't touch. User may have already migrated, or some
      // other process created the new folder. Log a warning but use the new
      // one (preserves the migration intent).
      console.warn('[userData migration] both old and new userData folders exist. Using new path; old path is now orphaned and may need manual cleanup:', oldPath);
    }
    // Always point Electron at the new path so subsequent app.getPath('userData')
    // calls return ~/Library/Application Support/shamsulalamx/ — works even
    // before the directory exists (Electron creates it on first write).
    app.setPath('userData', newPath);
  } catch (err) {
    // Migration failure is non-fatal — fall back to Electron's default
    // userData path (the OLD folder). User can still use the app; the
    // migration can be retried on next launch.
    console.error('[userData migration] failed (non-fatal):', err && err.message ? err.message : err);
  }
})();

const DEFAULT_DEV_URL = 'http://localhost:8888';
const GEMINI_MODEL = 'gemini-2.5-flash';
const GEMINI_ENDPOINT = `https://generativelanguage.googleapis.com/v1beta/models/${GEMINI_MODEL}:generateContent`;
let resolvedDevUrl = null; // set at startup; see startEmbeddedServer / NBME_ELECTRON_URL override

const BATCH_IMPORT_RUNNER = path.join(app.getAppPath(), 'tools', 'batch-import-center', 'run_pipeline_job.py');
const BATCH_IMPORT_REGISTRY = path.join(app.getAppPath(), 'tools', 'batch-import-center', 'pipeline_registry.json');
const BATCH_JOB_MANIFEST_VERSION = 'batch-import-job-v1';
const BATCH_JOB_HISTORY_VERSION = 'batch-import-job-history-v1';
const BATCH_QUEUE_VERSION = 'batch-import-queue-v1';
const activeBatchJobs = new Map();
const batchQueueWaiters = new Map();
let batchQueueRunning = false;
let batchReconciliationRunning = false;
let batchShutdownInProgress = false;

const singleInstanceLock = app.requestSingleInstanceLock();
if (!singleInstanceLock) {
  app.quit();
  process.exit(0);
}

ipcMain.handle('nbme:ai:get-status', async () => ({
  available: true,
  provider: 'gemini',
  model: GEMINI_MODEL,
  hasApiKey: !!process.env.GEMINI_API_KEY,
  desktopMode: true
}));

function readBatchRegistry() {
  const raw = fs.readFileSync(BATCH_IMPORT_REGISTRY, 'utf8');
  return JSON.parse(raw);
}

function batchHistoryPath() {
  const dir = path.join(app.getPath('userData'), 'batch-import-center');
  fs.mkdirSync(dir, { recursive: true });
  return path.join(dir, 'job-history.json');
}

function batchJobOutputRoot(jobId) {
  const outputRoot = path.join(app.getPath('userData'), 'batch-import-center', 'jobs', jobId);
  fs.mkdirSync(outputRoot, { recursive: true });
  return outputRoot;
}

function batchJobsRoot() {
  return path.join(app.getPath('userData'), 'batch-import-center', 'jobs');
}

function pathLivesUnder(rootPath, candidatePath) {
  const root = path.resolve(String(rootPath || ''));
  const candidate = path.resolve(String(candidatePath || ''));
  return candidate === root || (!path.relative(root, candidate).startsWith('..') && !path.isAbsolute(path.relative(root, candidate)));
}

function knownBatchQueueJob(jobId) {
  const cleanJobId = String(jobId || '').trim();
  if (!cleanJobId) return null;
  return readBatchQueue().jobs.find(job => job.jobId === cleanJobId) || null;
}

function knownBatchJobOutputRoot(job) {
  const outputRoot = String(job?.outputRoot || '').trim();
  if (!outputRoot || !pathLivesUnder(batchJobsRoot(), outputRoot)) return '';
  return path.resolve(outputRoot);
}

function knownBatchJobLogsPath(job) {
  const outputRoot = knownBatchJobOutputRoot(job);
  const logsPath = String(job?.logsPath || '').trim();
  if (!outputRoot || !logsPath || !pathLivesUnder(outputRoot, logsPath)) return '';
  if (path.basename(logsPath) !== 'queue-events.ndjson') return '';
  return path.resolve(logsPath);
}

function knownBatchJobReviewDraftPath(job) {
  const outputRoot = knownBatchJobOutputRoot(job);
  const draftPath = String(job?.draftPath || '').trim();
  if (!outputRoot || !draftPath || !pathLivesUnder(outputRoot, draftPath)) return '';
  if (!path.basename(draftPath).endsWith('_review_draft.json')) return '';
  return path.resolve(draftPath);
}

function knownBatchJobReviewDecisionsPath(job) {
  const outputRoot = knownBatchJobOutputRoot(job);
  const decisionsPath = String(job?.decisionsPath || '').trim();
  if (!outputRoot || !decisionsPath || !pathLivesUnder(outputRoot, decisionsPath)) return '';
  if (path.basename(decisionsPath) !== 'review_decisions.json') return '';
  return path.resolve(decisionsPath);
}

function knownBatchJobReviewEditsPath(job) {
  const outputRoot = knownBatchJobOutputRoot(job);
  const editsPath = String(job?.editsPath || '').trim();
  if (!outputRoot || !editsPath || !pathLivesUnder(outputRoot, editsPath)) return '';
  if (path.basename(editsPath) !== 'review_edits.json') return '';
  return path.resolve(editsPath);
}

function knownBatchJobReviewImportMarkerPath(job) {
  const outputRoot = knownBatchJobOutputRoot(job);
  if (!outputRoot) return '';
  return path.join(outputRoot, 'review', 'survivor_import_in_progress.json');
}

function batchReviewDir(job) {
  const outputRoot = knownBatchJobOutputRoot(job);
  if (!outputRoot) return '';
  const reviewDir = path.join(outputRoot, 'review');
  if (fs.existsSync(reviewDir) && fs.lstatSync(reviewDir).isSymbolicLink()) return '';
  return reviewDir;
}

function sanitizeBatchReviewDecisions(value) {
  const decisions = Array.isArray(value) ? value : [];
  return decisions.map(item => ({
    questionIndex: Number(item?.questionIndex),
    decision: item?.decision === 'accept' || item?.decision === 'reject' ? item.decision : 'pending'
  })).filter(item => Number.isInteger(item.questionIndex) && item.questionIndex > 0);
}

function batchReviewDecisionSummary(draft, decisions) {
  const candidateCount = Array.isArray(draft?.candidateQuestions) ? draft.candidateQuestions.length : 0;
  const byIndex = new Map(decisions.map(item => [item.questionIndex, item.decision]));
  let acceptedCount = 0;
  let rejectedCount = 0;
  for (let index = 1; index <= candidateCount; index += 1) {
    if (byIndex.get(index) === 'accept') acceptedCount += 1;
    if (byIndex.get(index) === 'reject') rejectedCount += 1;
  }
  return { acceptedCount, rejectedCount, pendingReviewCount: Math.max(0, candidateCount - acceptedCount - rejectedCount) };
}

function readKnownBatchReviewDraft(job) {
  const draftPath = knownBatchJobReviewDraftPath(job);
  if (!draftPath) return safeError('BATCH_REVIEW_DRAFT_REJECTED', 'Review draft path is not recorded under this durable queue job.');
  if (!fs.existsSync(draftPath) || fs.lstatSync(draftPath).isSymbolicLink() || !fs.statSync(draftPath).isFile()) {
    return safeError('BATCH_REVIEW_DRAFT_MISSING', 'Review draft file was not found.');
  }
  try {
    const draft = JSON.parse(fs.readFileSync(draftPath, 'utf8'));
    if (!draft || draft.draftVersion !== 1 || !Array.isArray(draft.candidateQuestions)) {
      return safeError('BATCH_REVIEW_DRAFT_INVALID', 'Review draft is not a supported durable draft.');
    }
    return { ok: true, draftPath, draft };
  } catch (err) {
    return safeError('BATCH_REVIEW_DRAFT_INVALID', err.message || String(err));
  }
}

function readKnownBatchReviewDecisions(job) {
  const decisionsPath = knownBatchJobReviewDecisionsPath(job);
  if (!decisionsPath || !fs.existsSync(decisionsPath) || fs.lstatSync(decisionsPath).isSymbolicLink() || !fs.statSync(decisionsPath).isFile()) {
    return [];
  }
  try {
    return sanitizeBatchReviewDecisions(JSON.parse(fs.readFileSync(decisionsPath, 'utf8'))?.decisions);
  } catch (_err) {
    return [];
  }
}

function readKnownBatchReviewEdits(job) {
  const editsPath = knownBatchJobReviewEditsPath(job) || path.join(batchReviewDir(job) || '', 'review_edits.json');
  if (!editsPath || !fs.existsSync(editsPath) || fs.lstatSync(editsPath).isSymbolicLink() || !fs.statSync(editsPath).isFile()) {
    return {};
  }
  try {
    const parsed = JSON.parse(fs.readFileSync(editsPath, 'utf8'));
    return parsed && typeof parsed.editedQuestions === 'object' && !Array.isArray(parsed.editedQuestions) ? parsed.editedQuestions : {};
  } catch (_err) {
    return {};
  }
}

function batchQueuePath() {
  const queueDir = path.join(app.getPath('userData'), 'batch-import-center', 'queue');
  fs.mkdirSync(queueDir, { recursive: true });
  return path.join(queueDir, 'jobs.json');
}

function corruptBatchQueuePath(queuePath) {
  const corruptPath = `${queuePath}.corrupt.${new Date().toISOString().replace(/[:.]/g, '-')}`;
  fs.renameSync(queuePath, corruptPath);
  console.error(`[NBME] Batch queue JSON is corrupt. Preserved original at: ${corruptPath}`);
  return corruptPath;
}

function readBatchQueueRaw() {
  const queuePath = batchQueuePath();
  if (!fs.existsSync(queuePath)) return { schemaVersion: BATCH_QUEUE_VERSION, jobs: [] };
  try {
    const parsed = JSON.parse(fs.readFileSync(queuePath, 'utf8'));
    if (!parsed || !Array.isArray(parsed.jobs)) throw new Error('Invalid queue shape');
    return { schemaVersion: parsed.schemaVersion || BATCH_QUEUE_VERSION, jobs: parsed.jobs.filter(job => job && typeof job === 'object') };
  } catch (err) {
    if (err instanceof SyntaxError) {
      try {
        corruptBatchQueuePath(queuePath);
      } catch (renameErr) {
        console.error(`[NBME] Batch queue JSON is corrupt and could not be renamed: ${renameErr.message || String(renameErr)}`);
      }
    } else {
      console.error(`[NBME] Batch queue could not be read: ${err.message || String(err)}`);
    }
    return { schemaVersion: BATCH_QUEUE_VERSION, jobs: [] };
  }
}

function readBatchQueue() {
  return readBatchQueueRaw();
}

function writeBatchQueue(queue) {
  const queuePath = batchQueuePath();
  const tmpPath = `${queuePath}.tmp`;
  fs.writeFileSync(tmpPath, JSON.stringify(queue, null, 2), 'utf8');
  fs.renameSync(tmpPath, queuePath);
}

function updateBatchQueueJob(jobId, patch) {
  const queue = readBatchQueue();
  const idx = queue.jobs.findIndex(job => job.jobId === jobId);
  if (idx < 0) return null;
  const existing = queue.jobs[idx];
  const cleanPatch = Object.fromEntries(Object.entries(patch || {}).filter(([, value]) => value !== undefined));
  const updated = {
    ...existing,
    ...cleanPatch,
    progress: cleanPatch.progress && typeof cleanPatch.progress === 'object' ? cleanPatch.progress : (existing.progress || {}),
    outputPaths: Array.isArray(cleanPatch.outputPaths) ? cleanPatch.outputPaths : (existing.outputPaths || []),
    warnings: Array.isArray(cleanPatch.warnings) ? cleanPatch.warnings : (existing.warnings || []),
    errors: Array.isArray(cleanPatch.errors) ? cleanPatch.errors : (existing.errors || []),
    updatedAt: new Date().toISOString()
  };
  queue.jobs[idx] = updated;
  writeBatchQueue(queue);
  emitBatchQueueChanged();
  return updated;
}

function persistBatchQueueJob(job) {
  const queue = readBatchQueue();
  const jobs = queue.jobs.filter(existing => existing.jobId !== job.jobId);
  jobs.push(job);
  writeBatchQueue({ schemaVersion: BATCH_QUEUE_VERSION, jobs });
  emitBatchQueueChanged();
  return job;
}

function removeBatchQueueJob(jobId) {
  const queue = readBatchQueue();
  const job = queue.jobs.find(item => item.jobId === jobId);
  if (!job) return null;
  if (!['completed', 'completed_with_review_required', 'completed_with_review', 'failed', 'canceled', 'interrupted'].includes(job.status)) return false;
  writeBatchQueue({
    schemaVersion: queue.schemaVersion || BATCH_QUEUE_VERSION,
    jobs: queue.jobs.filter(item => item.jobId !== jobId)
  });
  emitBatchQueueChanged();
  return job;
}

function emitBatchQueueChanged() {
  const payload = { queue: readBatchQueue() };
  BrowserWindow.getAllWindows().forEach(win => {
    if (!win.isDestroyed()) win.webContents.send('nbme:batch-import:queue-changed', payload);
  });
}

function appendBatchLog(logsPath, eventPayload) {
  try {
    fs.mkdirSync(path.dirname(logsPath), { recursive: true });
    fs.appendFileSync(logsPath, `${JSON.stringify(eventPayload)}\n`, 'utf8');
  } catch (_) {}
}

function writeBatchCompletionReport(outputRoot, report) {
  try {
    const reportPath = path.join(outputRoot, 'completion-report.json');
    fs.mkdirSync(path.dirname(reportPath), { recursive: true });
    fs.writeFileSync(reportPath, JSON.stringify(report || {}, null, 2), 'utf8');
    return reportPath;
  } catch (_) {
    return null;
  }
}

function readBatchJobHistoryRaw() {
  const historyPath = batchHistoryPath();
  if (!fs.existsSync(historyPath)) return { schemaVersion: BATCH_JOB_HISTORY_VERSION, jobs: [] };
  try {
    const parsed = JSON.parse(fs.readFileSync(historyPath, 'utf8'));
    if (!parsed || !Array.isArray(parsed.jobs)) throw new Error('Invalid history shape');
    return {
      schemaVersion: parsed.schemaVersion || BATCH_JOB_HISTORY_VERSION,
      jobs: parsed.jobs.filter(job => job && typeof job === 'object')
    };
  } catch (_) {
    return { schemaVersion: BATCH_JOB_HISTORY_VERSION, jobs: [] };
  }
}

function readBatchJobHistory() {
  return readBatchJobHistoryRaw();
}

function writeBatchJobHistory(history) {
  const historyPath = batchHistoryPath();
  const tmpPath = `${historyPath}.tmp`;
  fs.writeFileSync(tmpPath, JSON.stringify(history, null, 2), 'utf8');
  fs.renameSync(tmpPath, historyPath);
}

function persistBatchJobRecord(record) {
  const history = readBatchJobHistory();
  const jobs = history.jobs.filter(job => job.jobId !== record.jobId);
  jobs.unshift(record);
  writeBatchJobHistory({
    schemaVersion: BATCH_JOB_HISTORY_VERSION,
    jobs: jobs
      .sort((a, b) => String(b.updatedAt || b.createdAt || '').localeCompare(String(a.updatedAt || a.createdAt || '')))
      .slice(0, 100)
  });
  return record;
}

function updateBatchJobRecord(jobId, patch) {
  const history = readBatchJobHistory();
  const existing = history.jobs.find(job => job.jobId === jobId);
  if (!existing) return null;
  const cleanPatch = Object.fromEntries(
    Object.entries(patch || {}).filter(([, value]) => value !== undefined)
  );
  const updated = {
    ...existing,
    ...cleanPatch,
    warnings: Array.isArray(cleanPatch.warnings) ? cleanPatch.warnings : (existing.warnings || []),
    errors: Array.isArray(cleanPatch.errors) ? cleanPatch.errors : (existing.errors || []),
    outputPaths: Array.isArray(cleanPatch.outputPaths) ? cleanPatch.outputPaths : (existing.outputPaths || []),
    updatedAt: new Date().toISOString()
  };
  return persistBatchJobRecord(updated);
}

function findExistingAppReadyOutput(outputRoot) {
  if (!outputRoot || !fs.existsSync(outputRoot)) return '';
  const pending = [outputRoot];
  while (pending.length) {
    const current = pending.pop();
    let entries = [];
    try {
      if (fs.lstatSync(current).isSymbolicLink()) continue;
      entries = fs.readdirSync(current, { withFileTypes: true });
    } catch (_) {
      continue;
    }
    for (const entry of entries) {
      const entryPath = path.join(current, entry.name);
      if (entry.isSymbolicLink()) continue;
      if (entry.isDirectory()) {
        pending.push(entryPath);
      } else if (entry.isFile() && entry.name.endsWith('_app_ready.json')) {
        return entryPath;
      }
    }
  }
  return '';
}

function batchJobCompletionArtifacts(job) {
  const outputRoot = knownBatchJobOutputRoot(job);
  const reportPath = outputRoot ? path.join(outputRoot, 'completion-report.json') : '';
  const recordedReportPath = String(job?.reportPath || '').trim();
  const outputPaths = Array.isArray(job?.outputPaths) ? job.outputPaths.map(item => String(item || '').trim()).filter(Boolean) : [];
  const inputPaths = Array.isArray(job?.inputFiles) ? job.inputFiles.map(item => String(item?.path || '').trim()).filter(Boolean) : [];
  const appReadyCandidates = [...outputPaths, ...inputPaths];
  const appReadyPath = appReadyCandidates.find(outputPath => outputPath.endsWith('_app_ready.json') && fs.existsSync(outputPath)) || findExistingAppReadyOutput(outputRoot);
  const completionReportPath = (reportPath && fs.existsSync(reportPath)) ? reportPath : ((recordedReportPath && fs.existsSync(recordedReportPath)) ? recordedReportPath : '');
  return { completionReportPath, appReadyPath };
}

function completedBatchJobPatch(job, artifacts, now) {
  const outputPaths = Array.isArray(job.outputPaths) ? [...job.outputPaths] : [];
  if (artifacts.appReadyPath && !outputPaths.includes(artifacts.appReadyPath)) outputPaths.push(artifacts.appReadyPath);
  let reportStatus = '';
  if (artifacts.completionReportPath) {
    try {
      const report = JSON.parse(fs.readFileSync(artifacts.completionReportPath, 'utf8'));
      reportStatus = String(report?.status || '');
    } catch (_) {}
  }
  // v4.78: PRESERVE failed/canceled/interrupted statuses from the saved report
  // rather than forcing every reconciled job to 'completed'. Pre-v4.78 this
  // function ignored the real status — any job with a completion-report.json on
  // disk became 'completed' on the next app launch, even if it had been cancelled
  // or had failed. That's how a cancelled Mehlman job showed up as Status:
  // completed with Errors: [Job cancelled.] in the queue details.
  const normalizedReportStatus = reportStatus === 'cancelled' ? 'canceled' : reportStatus;
  const finalStatus = (normalizedReportStatus === 'failed'
    || normalizedReportStatus === 'canceled'
    || normalizedReportStatus === 'interrupted')
    ? normalizedReportStatus
    : (normalizedReportStatus === 'completed_with_review_required' || normalizedReportStatus === 'needs_review'
        ? 'completed_with_review_required'
        : 'completed');
  const progressMessage = finalStatus === 'completed_with_review_required'
    ? 'Batch job completed with review artifacts.'
    : (finalStatus === 'canceled'
        ? 'Batch job canceled by user.'
        : (finalStatus === 'failed'
            ? 'Batch job failed.'
            : (finalStatus === 'interrupted'
                ? 'Batch job interrupted before completion.'
                : 'Batch job completed.')));
  return {
    ...job,
    status: finalStatus,
    finishedAt: job.finishedAt || job.completedAt || now,
    completedAt: job.completedAt || job.finishedAt || now,
    updatedAt: now,
    outputPaths,
    reportPath: job.reportPath || artifacts.completionReportPath || null,
    progress: job.progress || {
      phase: finalStatus,
      message: progressMessage,
      updatedAt: now
    }
  };
}

function interruptedBatchJobPatch(job, now) {
  return {
    ...job,
    status: 'interrupted',
    finishedAt: job.finishedAt || job.completedAt || now,
    completedAt: job.completedAt || job.finishedAt || now,
    updatedAt: now,
    errors: [...(job.errors || []), 'The app closed before this queued job reported completion.']
  };
}

function reconcileQueueAndHistoryOnStartup() {
  if (batchReconciliationRunning) return;
  batchReconciliationRunning = true;
  try {
    const queue = readBatchQueueRaw();
    const history = readBatchJobHistoryRaw();
    let queueChanged = false;
    let historyChanged = false;
    const now = new Date().toISOString();

    queue.jobs = queue.jobs.map(job => {
      const artifacts = batchJobCompletionArtifacts(job);
      if (artifacts.completionReportPath || artifacts.appReadyPath) {
        const updated = completedBatchJobPatch(job, artifacts, now);
        if (JSON.stringify(updated) !== JSON.stringify(job)) queueChanged = true;
        return updated;
      }
      if (job.status === 'running' && !activeBatchJobs.has(job.jobId)) {
        cleanupTrackedBatchProcess(job, 'startup stale queue job');
        queueChanged = true;
        return interruptedBatchJobPatch(job, now);
      }
      return job;
    });

    history.jobs = history.jobs.map(job => {
      const queueJob = queue.jobs.find(item => item.jobId === job.jobId);
      const artifacts = batchJobCompletionArtifacts(queueJob || job);
      if (artifacts.completionReportPath || artifacts.appReadyPath || queueJob?.status === 'completed') {
        const updated = completedBatchJobPatch({ ...job, outputRoot: job.outputRoot || queueJob?.outputRoot, outputPaths: job.outputPaths || queueJob?.outputPaths }, artifacts, now);
        if (JSON.stringify(updated) !== JSON.stringify(job)) historyChanged = true;
        return updated;
      }
      if ((job.status === 'running' || job.status === 'queued') && queueJob?.status === 'interrupted') {
        historyChanged = true;
        return interruptedBatchJobPatch(job, now);
      }
      if (job.status === 'running' && !queueJob) {
        cleanupTrackedBatchProcess(job, 'startup stale history job');
        historyChanged = true;
        return interruptedBatchJobPatch(job, now);
      }
      return job;
    });

    if (queueChanged) writeBatchQueue(queue);
    if (historyChanged) writeBatchJobHistory({ schemaVersion: BATCH_JOB_HISTORY_VERSION, jobs: history.jobs });
  } finally {
    batchReconciliationRunning = false;
  }
}

function reconcileStaleBatchJobs() {
  reconcileQueueAndHistoryOnStartup();
}

function reconcileInterruptedBatchQueue() {
  reconcileQueueAndHistoryOnStartup();
}

function batchProcessRegistryPath(job) {
  const outputRoot = knownBatchJobOutputRoot(job);
  if (!outputRoot) return '';
  return path.join(outputRoot, 'process_registry.json');
}

function readBatchProcessRegistry(job) {
  const registryPath = batchProcessRegistryPath(job);
  if (!registryPath || !fs.existsSync(registryPath)) return null;
  try {
    const parsed = JSON.parse(fs.readFileSync(registryPath, 'utf8'));
    if (!parsed || typeof parsed !== 'object') return null;
    const pid = Number(parsed.pid);
    if (!Number.isFinite(pid)) return null;
    return {
      jobId: String(parsed.jobId || job?.jobId || ''),
      pid,
      startedAt: Number(parsed.startedAt) || null,
      status: String(parsed.status || '')
    };
  } catch (err) {
    console.warn(`[NBME] Batch process registry ignored safely (${job?.jobId || 'unknown'}): ${err.message || String(err)}`);
    return null;
  }
}

function writeBatchProcessRegistry(job, pid) {
  const registryPath = batchProcessRegistryPath(job);
  if (!registryPath || !pid || typeof pid !== 'number') {
    console.warn(`[NBME] Batch process registry write skipped (${job?.jobId || 'unknown'}): pid missing.`);
    return;
  }
  try {
    fs.mkdirSync(path.dirname(registryPath), { recursive: true });
    fs.writeFileSync(registryPath, JSON.stringify({
      jobId: job.jobId,
      pid,
      startedAt: Date.now(),
      status: 'running'
    }, null, 2), 'utf8');
  } catch (err) {
    console.warn(`[NBME] Batch process registry write failed safely (${job?.jobId || 'unknown'}): ${err.message || String(err)}`);
  }
}

function updateBatchProcessRegistryStatus(job, status) {
  const registryPath = batchProcessRegistryPath(job);
  if (!registryPath || !fs.existsSync(registryPath)) return;
  try {
    const parsed = JSON.parse(fs.readFileSync(registryPath, 'utf8'));
    fs.writeFileSync(registryPath, JSON.stringify({ ...parsed, status }, null, 2), 'utf8');
  } catch (err) {
    console.warn(`[NBME] Batch process registry update failed safely (${job?.jobId || 'unknown'}): ${err.message || String(err)}`);
  }
}

function processRefFromPid(pid) {
  return Number.isFinite(Number(pid)) ? { pid: Number(pid) } : null;
}

function safeKillProcessGroup(proc, signal, context) {
  const pid = proc?.pid;
  if (!pid || typeof pid !== 'number') {
    console.warn(`[NBME] Batch process kill skipped (${context || signal}): pid missing.`);
    return false;
  }
  try {
    process.kill(pid, 0);
  } catch (err) {
    console.warn(`[NBME] Batch process kill skipped (${context || signal}): pid ${pid} is not alive (${err.message || String(err)}).`);
    return false;
  }
  try {
    process.kill(-pid, signal);
    return true;
  } catch (err) {
    console.warn(`[NBME] Batch process group kill failed safely (${context || signal}): pid ${pid}, signal ${signal} (${err.message || String(err)}).`);
    return false;
  }
}

function safeKillProcess(proc, signal, context) {
  const pid = proc?.pid;
  if (!pid || typeof pid !== 'number') {
    console.warn(`[NBME] Batch process kill skipped (${context || signal}): pid missing.`);
    return false;
  }
  try {
    if (typeof proc.kill === 'function') {
      proc.kill(signal);
      return true;
    }
  } catch (err) {
    console.warn(`[NBME] Batch process kill failed safely (${context || signal}): pid ${pid}, signal ${signal} (${err.message || String(err)}).`);
  }
  return false;
}

function cleanupTrackedBatchProcess(job, context) {
  const registry = readBatchProcessRegistry(job);
  if (!registry?.pid) {
    console.warn(`[NBME] Batch process registry cleanup skipped (${context || job?.jobId || 'unknown'}): pid missing.`);
    return;
  }
  const procRef = processRefFromPid(registry.pid);
  const label = `${context || 'tracked cleanup'} ${job?.jobId || registry.jobId || ''}`.trim();
  try {
    const signalled = safeKillProcessGroup(procRef, 'SIGTERM', label);
    if (signalled) {
      setTimeout(() => {
        try {
          safeKillProcessGroup(procRef, 'SIGKILL', `${label} fallback`);
        } catch (err) {
          console.warn(`[NBME] Batch process registry fallback cleanup failed safely (${label}): ${err.message || String(err)}`);
        }
      }, 1500);
    }
  } catch (err) {
    console.warn(`[NBME] Batch process registry cleanup failed safely (${label}): ${err.message || String(err)}`);
  }
}

function cleanupTrackedBatchProcessesForJobs(jobs, context) {
  for (const job of jobs || []) {
    try {
      cleanupTrackedBatchProcess(job, context);
    } catch (err) {
      console.warn(`[NBME] Batch process registry cleanup failed safely (${job?.jobId || 'unknown'}): ${err.message || String(err)}`);
    }
  }
}

function interruptActiveBatchJobsForShutdown() {
  const now = new Date().toISOString();
  activeBatchJobs.forEach((active, jobId) => {
    updateBatchQueueJob(jobId, {
      status: 'interrupted',
      finishedAt: now,
      errors: [...(active.errors || []), 'The app closed before this queued job reported completion.']
    });
    updateBatchJobRecord(jobId, {
      status: 'interrupted',
      completedAt: now,
      errors: [...(active.errors || []), 'The app closed before this job reported completion.']
    });
    if (active.proc?.pid) {
      safeKillProcessGroup(active.proc, 'SIGTERM', `shutdown ${jobId}`) || safeKillProcess(active.proc, 'SIGTERM', `shutdown ${jobId}`);
    } else {
      console.warn(`[NBME] Batch process kill skipped (shutdown ${jobId}): pid missing.`);
    }
  });
}

function initialBatchJobRecord(manifest, source, manifestPath) {
  const now = new Date().toISOString();
  return {
    jobId: manifest.jobId,
    status: 'queued',
    createdAt: manifest.createdAt || now,
    startedAt: null,
    completedAt: null,
    updatedAt: now,
    sourceType: manifest.sourceType,
    sourceLabel: source?.label || manifest.sourceType,
    dryRun: !!manifest.dryRun,
    targetFolderId: manifest.destination?.folderId || '',
    targetTestName: manifest.destination?.testName || '',
    inputPaths: (manifest.inputs || []).map(item => item.path).filter(Boolean),
    outputRoot: manifest.outputRoot || '',
    manifestPath,
    runtimeSeconds: null,
    currentStage: 'preflight',
    outputPaths: [],
    warnings: [],
    errors: [],
    report: null,
    importedTestId: null,
    importedTestName: null
  };
}

function readEnvLocal() {
  const envPath = path.join(app.getAppPath(), '.env.local');
  if (!fs.existsSync(envPath) || !fs.statSync(envPath).isFile()) return {};
  const parsed = {};
  const lines = fs.readFileSync(envPath, 'utf8').split(/\r?\n/);
  for (const line of lines) {
    const trimmed = line.trim();
    if (!trimmed || trimmed.startsWith('#')) continue;
    const match = trimmed.match(/^([A-Za-z_][A-Za-z0-9_]*)=(.*)$/);
    if (!match) continue;
    let value = match[2].trim();
    if ((value.startsWith('"') && value.endsWith('"')) || (value.startsWith("'") && value.endsWith("'"))) {
      value = value.slice(1, -1);
    }
    parsed[match[1]] = value;
  }
  return parsed;
}

function batchEnvFromLocalSources() {
  const env = { ...process.env };
  if (!env.GEMINI_API_KEY) {
    const localEnv = readEnvLocal();
    if (localEnv.GEMINI_API_KEY) env.GEMINI_API_KEY = localEnv.GEMINI_API_KEY;
  }
  return env;
}

async function shellHasGeminiApiKey() {
  return await new Promise(resolve => {
    const proc = spawn('/bin/zsh', ['-lc', 'source ~/.zshrc >/dev/null 2>&1 || true; test -n "$GEMINI_API_KEY"'], {
      cwd: app.getAppPath(),
      env: process.env,
      stdio: ['ignore', 'ignore', 'ignore']
    });
    proc.on('error', () => resolve(false));
    proc.on('close', code => resolve(code === 0));
  });
}

async function resolveBatchEnvironment(requiresGemini) {
  const env = batchEnvFromLocalSources();
  if (!requiresGemini || env.GEMINI_API_KEY) {
    return { ok: true, env, useLoginShell: false };
  }
  if (await shellHasGeminiApiKey()) {
    return { ok: true, env: process.env, useLoginShell: true };
  }
  return safeError(
    'BATCH_GEMINI_KEY_UNAVAILABLE',
    'GEMINI_API_KEY is not available to the Electron batch process.'
  );
}

function sanitizeBatchJobPayload(payload, source) {
  const sourceType = String(payload?.sourceType || '').trim();
  const inputPaths = Array.isArray(payload?.inputPaths) ? payload.inputPaths : [];
  const folderId = String(payload?.destination?.folderId || '').trim();
  const testName = String(payload?.destination?.testName || '').trim();
  const existingOutputValidation = payload?.existingOutputValidation === true;
  const dryRun = payload?.dryRun !== false;
  const executePipeline = payload?.executePipeline === true;
  // Advanced Mode toggle (v5.3): when the UI checkbox is on AND the source
  // declared supportsAdvancedMode in the registry, the Python runner appends
  // each step's advancedArgs to its subprocess command. For OME today this
  // engages --v5 on ome_profile_runner.py. Sources without supportsAdvancedMode
  // ignore the flag — keeps a single UI surface for forward compatibility.
  const advancedMode = payload?.advancedMode === true && !!source?.supportsAdvancedMode;
  // v5.6: per-job knobs the user sets in the Advanced Mode UI panel.
  // v5.7: only include advancedConfig when the source declares
  // supportsChunkControls. Sources that subprocess into the
  // lecture-slide generator (Fast Facts, Emma Holliday) use
  // slide-based allocation, not character chunking, and their CLI
  // doesn't recognize --chunk-size / --questions-per-chunk. Sending
  // those flags would cause an argparse error in the downstream and
  // a hard pipeline failure. Gating the manifest field at the
  // sanitizer layer keeps the run_pipeline_job.py side simple
  // (manifest_advanced_config_args returns [] for missing config).
  const advancedConfigInput = (payload?.advancedConfig && typeof payload.advancedConfig === 'object')
    ? payload.advancedConfig
    : {};
  const supportsChunkControls = advancedMode && !!source?.supportsChunkControls;
  // v5.10: Refined is an opt-in quality tier WITHIN Advanced Mode. Unlike the
  // chunk knobs it is NOT chunk-specific — it applies to every organic v5
  // generator (OME, Fast Facts, Emma, uWorld family, …) — so it is gated only
  // on advancedMode, never on supportsChunkControls. run_pipeline_job turns
  // advancedConfig.refined into V5_REFINED=1 in the generator subprocess env,
  // which v5_pipeline reads via _default_mode().
  const refined = advancedMode && advancedConfigInput.refined === true;
  const advancedConfig = {
    ...(supportsChunkControls
      ? {
          chunkSize: Math.max(0, Math.floor(Number(advancedConfigInput.chunkSize) || 0)),
          questionsPerChunk: Math.max(0, Math.floor(Number(advancedConfigInput.questionsPerChunk) || 0)),
        }
      : {}),
    ...(refined ? { refined: true } : {}),
  };

  if (!sourceType) throw new Error('sourceType is required.');
  if (!inputPaths.length) throw new Error('At least one input file is required.');

  const jobId = `batch-${Date.now().toString(36)}-${Math.random().toString(36).slice(2, 8)}`;
  return {
    manifestVersion: BATCH_JOB_MANIFEST_VERSION,
    jobId,
    sourceType,
    inputs: inputPaths.map(inputPath => ({ path: String(inputPath || '').trim() })).filter(item => item.path),
    requiresGemini: existingOutputValidation ? false : !!source?.requiresGemini,
    dryRun,
    executePipeline,
    existingOutputValidation,
    advancedMode,
    advancedConfig,
    destination: { folderId, testName },
    outputRoot: batchJobOutputRoot(jobId),
    createdAt: new Date().toISOString()
  };
}

function writeBatchManifest(manifest) {
  const jobDir = manifest.outputRoot || path.join(os.tmpdir(), 'nbme-batch-import-center');
  fs.mkdirSync(jobDir, { recursive: true });
  const manifestPath = path.join(jobDir, `${manifest.jobId}.json`);
  fs.writeFileSync(manifestPath, JSON.stringify(manifest, null, 2), 'utf8');
  return manifestPath;
}

function queueJobFromManifest(manifest, source, payload, manifestPath) {
  const now = new Date().toISOString();
  const outputRoot = manifest.outputRoot || batchJobOutputRoot(manifest.jobId);
  return {
    jobId: manifest.jobId,
    batchId: String(payload?.batchId || '').trim() || null,
    sourceType: manifest.sourceType,
    sourceLabel: source?.label || manifest.sourceType,
    inputFiles: (manifest.inputs || []).map(item => ({ path: item.path })),
    destinationFolderId: manifest.destination?.folderId || '',
    testName: manifest.destination?.testName || '',
    runMode: String(payload?.runMode || '').trim(),
    dryRun: !!manifest.dryRun,
    executePipeline: !!manifest.executePipeline,
    existingOutputValidation: !!manifest.existingOutputValidation,
    requiresGemini: !!manifest.requiresGemini,
    status: 'pending',
    progress: { phase: 'pending', message: 'Waiting in queue.', updatedAt: now },
    createdAt: manifest.createdAt || now,
    updatedAt: now,
    startedAt: null,
    finishedAt: null,
    outputRoot,
    outputPaths: [],
    warnings: [],
    errors: [],
    reportPath: null,
    logsPath: path.join(outputRoot, 'queue-events.ndjson'),
    importedTestId: null,
    manifestPath,
    manifest,
    lastHeartbeatAt: null
  };
}

ipcMain.handle('nbme:batch-import:get-registry', async () => {
  try {
    return { ok: true, registry: readBatchRegistry() };
  } catch (err) {
    return safeError('BATCH_REGISTRY_UNAVAILABLE', err.message || String(err));
  }
});

ipcMain.handle('nbme:batch-import:get-history', async () => {
  try {
    reconcileStaleBatchJobs();
    return { ok: true, history: readBatchJobHistory() };
  } catch (err) {
    return safeError('BATCH_HISTORY_UNAVAILABLE', err.message || String(err));
  }
});

ipcMain.handle('nbme:batch-import:get-queue', async () => {
  try {
    reconcileInterruptedBatchQueue();
    scheduleBatchQueue();
    return { ok: true, queue: readBatchQueue() };
  } catch (err) {
    return safeError('BATCH_QUEUE_UNAVAILABLE', err.message || String(err));
  }
});

ipcMain.handle('nbme:batch-import:read-queue-job-logs', async (_event, payload) => {
  try {
    const job = knownBatchQueueJob(payload?.jobId);
    if (!job) return safeError('BATCH_QUEUE_JOB_NOT_FOUND', 'Queued batch job was not found.');
    const logsPath = knownBatchJobLogsPath(job);
    if (!logsPath) return safeError('BATCH_QUEUE_LOG_REJECTED', 'Job log path is not a known durable queue log.');
    if (!fs.existsSync(logsPath)) return { ok: true, jobId: job.jobId, logsPath, lines: [] };
    if (fs.lstatSync(logsPath).isSymbolicLink() || !fs.statSync(logsPath).isFile()) {
      return safeError('BATCH_QUEUE_LOG_MISSING', 'Job log file was not found.');
    }
    const requestedLimit = Number(payload?.limit || 80);
    const limit = Math.max(1, Math.min(200, Number.isFinite(requestedLimit) ? requestedLimit : 80));
    const lines = fs.readFileSync(logsPath, 'utf8')
      .split(/\r?\n/)
      .filter(Boolean)
      .slice(-limit);
    return { ok: true, jobId: job.jobId, logsPath, lines };
  } catch (err) {
    return safeError('BATCH_QUEUE_LOG_READ_FAILED', err.message || String(err));
  }
});

ipcMain.handle('nbme:batch-import:read-review-draft', async (_event, payload) => {
  try {
    const job = knownBatchQueueJob(payload?.jobId);
    if (!job) return safeError('BATCH_QUEUE_JOB_NOT_FOUND', 'Queued batch job was not found.');
    const read = readKnownBatchReviewDraft(job);
    if (!read.ok) return read;
    return {
      ok: true,
      jobId: job.jobId,
      draftPath: read.draftPath,
      outputRoot: knownBatchJobOutputRoot(job),
      importedTestId: job.importedTestId || null,
      reviewSummary: job.reviewSummary || null,
      decisions: readKnownBatchReviewDecisions(job),
      editedQuestions: readKnownBatchReviewEdits(job),
      draft: read.draft
    };
  } catch (err) {
    return safeError('BATCH_REVIEW_DRAFT_READ_FAILED', err.message || String(err));
  }
});

ipcMain.handle('nbme:batch-import:save-review-edits', async (_event, payload) => {
  try {
    const job = knownBatchQueueJob(payload?.jobId);
    if (!job) return safeError('BATCH_QUEUE_JOB_NOT_FOUND', 'Queued batch job was not found.');
    const read = readKnownBatchReviewDraft(job);
    if (!read.ok) return read;
    const reviewDir = batchReviewDir(job);
    if (!reviewDir) return safeError('BATCH_REVIEW_OUTPUT_REJECTED', 'Review output root is not a known durable queue job.');
    const rawEdits = payload?.editedQuestions && typeof payload.editedQuestions === 'object' && !Array.isArray(payload.editedQuestions)
      ? payload.editedQuestions
      : {};
    const candidateCount = Array.isArray(read.draft.candidateQuestions) ? read.draft.candidateQuestions.length : 0;
    const editedQuestions = {};
    Object.entries(rawEdits).forEach(([key, value]) => {
      const index = Number(key);
      if (Number.isInteger(index) && index > 0 && index <= candidateCount && value && typeof value === 'object' && !Array.isArray(value)) {
        editedQuestions[String(index)] = value;
      }
    });
    fs.mkdirSync(reviewDir, { recursive: true });
    const editsPath = path.join(reviewDir, 'review_edits.json');
    fs.writeFileSync(editsPath, JSON.stringify({
      editsVersion: 1,
      jobId: job.jobId,
      draftPath: read.draftPath,
      savedAt: new Date().toISOString(),
      editedQuestions
    }, null, 2), 'utf8');
    updateBatchQueueJob(job.jobId, { editsPath });
    updateBatchJobRecord(job.jobId, { editsPath });
    return { ok: true, jobId: job.jobId, editsPath, editedQuestions };
  } catch (err) {
    return safeError('BATCH_REVIEW_EDITS_SAVE_FAILED', err.message || String(err));
  }
});

ipcMain.handle('nbme:batch-import:save-review-decisions', async (_event, payload) => {
  try {
    const job = knownBatchQueueJob(payload?.jobId);
    if (!job) return safeError('BATCH_QUEUE_JOB_NOT_FOUND', 'Queued batch job was not found.');
    const read = readKnownBatchReviewDraft(job);
    if (!read.ok) return read;
    const decisions = sanitizeBatchReviewDecisions(payload?.decisions);
    const reviewDir = batchReviewDir(job);
    if (!reviewDir) return safeError('BATCH_REVIEW_OUTPUT_REJECTED', 'Review output root is not a known durable queue job.');
    fs.mkdirSync(reviewDir, { recursive: true });
    const decisionsPath = path.join(reviewDir, 'review_decisions.json');
    const reviewSummary = batchReviewDecisionSummary(read.draft, decisions);
    fs.writeFileSync(decisionsPath, JSON.stringify({
      decisionsVersion: 1,
      jobId: job.jobId,
      draftPath: read.draftPath,
      savedAt: new Date().toISOString(),
      decisions,
      reviewSummary
    }, null, 2), 'utf8');
    updateBatchQueueJob(job.jobId, { decisionsPath, reviewSummary });
    updateBatchJobRecord(job.jobId, { decisionsPath, reviewSummary });
    return { ok: true, jobId: job.jobId, decisionsPath, reviewSummary };
  } catch (err) {
    return safeError('BATCH_REVIEW_DECISIONS_SAVE_FAILED', err.message || String(err));
  }
});

// Assemble explanationSections from raw Gemini-shape fields. The lecture-slide
// generator does this in build_explanation_sections() before producing app-ready
// JSON; reviewed survivor questions skip that step because they come straight
// from the review draft's candidateQuestions list. Without this assembly the
// renderer (which reads explanationSections) shows an empty explanation panel
// for accepted-review questions, even though the raw correctExplanation and
// incorrectExplanations fields are present on the question object.
function assembleReviewedQuestionExplanationSections(question) {
  const sections = [];
  const correct = String((question && question.correctExplanation) || '').trim();
  if (correct) {
    sections.push({ heading: 'Correct Answer Explanation', body: [correct] });
  }
  const rawIncorrect = (question && Array.isArray(question.incorrectExplanations))
    ? question.incorrectExplanations
    : [];
  const incorrectLines = [];
  for (const entry of rawIncorrect) {
    if (!entry || typeof entry !== 'object') continue;
    const label = String(entry.label || '').trim();
    const text = String(entry.explanation || '').trim();
    if (!text) continue;
    incorrectLines.push(label ? `${label}: ${text}` : text);
  }
  if (incorrectLines.length) {
    sections.push({ heading: 'Incorrect Answer Explanation', body: incorrectLines });
  }
  const edu = String((question && question.educationalObjective) || '').trim();
  if (edu) {
    sections.push({ heading: 'Educational Objective', body: [edu] });
  }
  return sections;
}

// Bring a reviewed-survivor question up to the canonical app-ready shape the
// renderer expects: assembled explanationSections (when the question does not
// already carry them), and empty arrays for the figure/image/table fields that
// downstream validators and the import path treat as required.
function canonicalizeReviewedSurvivorQuestion(question, offset) {
  const alreadyAssembled = Array.isArray(question && question.explanationSections)
    && question.explanationSections.length > 0;
  const assembled = alreadyAssembled
    ? question.explanationSections
    : assembleReviewedQuestionExplanationSections(question || {});
  return {
    ...(question || {}),
    questionNumber: offset + 1,
    explanationSections: assembled,
    figureRefs: Array.isArray(question && question.figureRefs) ? question.figureRefs : [],
    images: Array.isArray(question && question.images) ? question.images : [],
    explanationImages: Array.isArray(question && question.explanationImages) ? question.explanationImages : [],
    tables: Array.isArray(question && question.tables) ? question.tables : [],
    hasEmbeddedFigure: (question && question.hasEmbeddedFigure === true)
  };
}

ipcMain.handle('nbme:batch-import:write-accepted-review-survivors', async (_event, payload) => {
  try {
    const job = knownBatchQueueJob(payload?.jobId);
    if (!job) return safeError('BATCH_QUEUE_JOB_NOT_FOUND', 'Queued batch job was not found.');
    if (job.acceptedSurvivorsImportedTestId) return safeError('BATCH_REVIEW_ALREADY_IMPORTED', 'Accepted review questions were already imported for this job.');
    const read = readKnownBatchReviewDraft(job);
    if (!read.ok) return read;
    const decisions = sanitizeBatchReviewDecisions(payload?.decisions);
    const savedEdits = readKnownBatchReviewEdits(job);
    const editedQuestions = {
      ...savedEdits,
      ...(payload?.editedQuestions && typeof payload.editedQuestions === 'object' && !Array.isArray(payload.editedQuestions) ? payload.editedQuestions : {})
    };
    const acceptedIndexes = new Set(decisions.filter(item => item.decision === 'accept').map(item => item.questionIndex));
    const questions = read.draft.candidateQuestions
      .map((question, offset) => {
        const index = offset + 1;
        const edited = editedQuestions[String(index)];
        return edited && typeof edited === 'object' && !Array.isArray(edited) ? edited : question;
      })
      .filter((_question, offset) => acceptedIndexes.has(offset + 1));
    if (!questions.length) return safeError('BATCH_REVIEW_NO_ACCEPTED', 'Accept at least one draft question before import.');
    const reviewDir = batchReviewDir(job);
    if (!reviewDir) return safeError('BATCH_REVIEW_OUTPUT_REJECTED', 'Review output root is not a known durable queue job.');
    fs.mkdirSync(reviewDir, { recursive: true });
    const survivorPath = path.join(reviewDir, 'accepted_survivors_app_ready.json');
    const payloadJson = {
      schemaVersion: read.draft.schemaVersion || 'nbme-gemini-json-v3',
      sourceFormat: read.draft.sourceFormat || 'mixed',
      testTitle: String(payload?.testName || job.testName || 'Reviewed Batch Import').trim() || 'Reviewed Batch Import',
      questions: questions.map((question, offset) => canonicalizeReviewedSurvivorQuestion(question, offset))
    };
    fs.writeFileSync(survivorPath, JSON.stringify(payloadJson, null, 2), 'utf8');
    updateBatchQueueJob(job.jobId, { acceptedSurvivorsPath: survivorPath });
    updateBatchJobRecord(job.jobId, { acceptedSurvivorsPath: survivorPath });
    return { ok: true, jobId: job.jobId, survivorPath, acceptedQuestionCount: questions.length };
  } catch (err) {
    return safeError('BATCH_REVIEW_SURVIVORS_WRITE_FAILED', err.message || String(err));
  }
});

ipcMain.handle('nbme:batch-import:begin-review-survivor-import', async (_event, payload) => {
  try {
    const job = knownBatchQueueJob(payload?.jobId);
    if (!job) return safeError('BATCH_QUEUE_JOB_NOT_FOUND', 'Queued batch job was not found.');
    if (job.acceptedSurvivorsImportedTestId) return safeError('BATCH_REVIEW_ALREADY_IMPORTED', 'Accepted review questions were already imported for this job.');
    const reviewDir = batchReviewDir(job);
    if (!reviewDir) return safeError('BATCH_REVIEW_OUTPUT_REJECTED', 'Review output root is not a known durable queue job.');
    fs.mkdirSync(reviewDir, { recursive: true });
    const markerPath = knownBatchJobReviewImportMarkerPath(job);
    if (fs.existsSync(markerPath)) {
      return safeError('BATCH_REVIEW_IMPORT_IN_PROGRESS', 'Accepted survivor import is already in progress or was interrupted. Open job artifacts before retrying to avoid duplicate insertion.');
    }
    fs.writeFileSync(markerPath, JSON.stringify({
      markerVersion: 1,
      jobId: job.jobId,
      survivorPath: String(payload?.survivorPath || ''),
      startedAt: new Date().toISOString(),
      status: 'in_progress'
    }, null, 2), 'utf8');
    updateBatchQueueJob(job.jobId, { importInProgressPath: markerPath });
    updateBatchJobRecord(job.jobId, { importInProgressPath: markerPath });
    return { ok: true, jobId: job.jobId, markerPath };
  } catch (err) {
    return safeError('BATCH_REVIEW_IMPORT_GUARD_FAILED', err.message || String(err));
  }
});

ipcMain.handle('nbme:batch-import:finish-review-survivor-import', async (_event, payload) => {
  try {
    const job = knownBatchQueueJob(payload?.jobId);
    if (!job) return safeError('BATCH_QUEUE_JOB_NOT_FOUND', 'Queued batch job was not found.');
    const markerPath = knownBatchJobReviewImportMarkerPath(job);
    if (!markerPath) return safeError('BATCH_REVIEW_IMPORT_MARKER_REJECTED', 'Review import marker path is not under this durable queue job.');
    const succeeded = payload?.succeeded === true;
    if (succeeded) {
      fs.writeFileSync(markerPath, JSON.stringify({
        markerVersion: 1,
        jobId: job.jobId,
        survivorPath: String(payload?.survivorPath || ''),
        finishedAt: new Date().toISOString(),
        importedTestId: String(payload?.importedTestId || ''),
        status: 'imported'
      }, null, 2), 'utf8');
      return { ok: true, jobId: job.jobId, markerPath };
    }
    if (fs.existsSync(markerPath) && !fs.lstatSync(markerPath).isSymbolicLink()) fs.unlinkSync(markerPath);
    updateBatchQueueJob(job.jobId, { importInProgressPath: null });
    updateBatchJobRecord(job.jobId, { importInProgressPath: null });
    return { ok: true, jobId: job.jobId, cleared: true };
  } catch (err) {
    return safeError('BATCH_REVIEW_IMPORT_MARKER_FINISH_FAILED', err.message || String(err));
  }
});

ipcMain.handle('nbme:batch-import:open-queue-job-artifacts', async (_event, payload) => {
  try {
    const job = knownBatchQueueJob(payload?.jobId);
    if (!job) return safeError('BATCH_QUEUE_JOB_NOT_FOUND', 'Queued batch job was not found.');
    const outputRoot = knownBatchJobOutputRoot(job);
    if (!outputRoot) return safeError('BATCH_QUEUE_OUTPUT_REJECTED', 'Job output folder is not under the durable Batch Import jobs root.');
    if (!fs.existsSync(outputRoot) || !fs.statSync(outputRoot).isDirectory()) {
      return safeError('BATCH_QUEUE_OUTPUT_MISSING', 'Job output folder was not found.');
    }
    const openError = await shell.openPath(outputRoot);
    if (openError) return safeError('BATCH_QUEUE_OUTPUT_OPEN_FAILED', openError);
    return { ok: true, jobId: job.jobId, outputRoot };
  } catch (err) {
    return safeError('BATCH_QUEUE_OUTPUT_OPEN_FAILED', err.message || String(err));
  }
});

ipcMain.handle('nbme:batch-import:remove-queue-job', async (_event, payload) => {
  try {
    const jobId = String(payload?.jobId || '').trim();
    if (!jobId) return safeError('BATCH_JOB_ID_REQUIRED', 'jobId is required.');
    const removed = removeBatchQueueJob(jobId);
    if (removed === null) return safeError('BATCH_QUEUE_JOB_NOT_FOUND', 'Queued batch job was not found.');
    if (removed === false) {
      return safeError('BATCH_QUEUE_JOB_NOT_REMOVABLE', 'Only completed, failed, canceled, or interrupted jobs can be removed from the queue list.');
    }
    return { ok: true, job: removed, queue: readBatchQueue() };
  } catch (err) {
    return safeError('BATCH_QUEUE_REMOVE_FAILED', err.message || String(err));
  }
});

ipcMain.handle('nbme:batch-import:update-job-report', async (_event, payload) => {
  try {
    const jobId = String(payload?.jobId || '').trim();
    if (!jobId) return safeError('BATCH_JOB_ID_REQUIRED', 'jobId is required.');
    const report = payload?.report && typeof payload.report === 'object' ? payload.report : {};
    const updated = updateBatchJobRecord(jobId, {
      status: report.status || payload?.status || undefined,
      completedAt: report.completedAt || undefined,
      runtimeSeconds: Number.isFinite(report.runtimeSeconds) ? report.runtimeSeconds : undefined,
      outputPaths: Array.isArray(report.outputPaths) ? report.outputPaths : undefined,
      warnings: Array.isArray(report.warnings) ? report.warnings : undefined,
      errors: Array.isArray(report.errors) ? report.errors : undefined,
      importedTestId: report.importedTestId || null,
      importedTestName: report.importedTestName || null,
      importedAt: report.importedAt || undefined,
      importedAcceptedQuestionCount: Number.isFinite(report.importedAcceptedQuestionCount) ? report.importedAcceptedQuestionCount : undefined,
      acceptedSurvivorsImportedTestId: report.acceptedSurvivorsImportedTestId || undefined,
      acceptedSurvivorsImportedAt: report.acceptedSurvivorsImportedAt || undefined,
      decisionsPath: report.decisionsPath || undefined,
      acceptedSurvivorsPath: report.acceptedSurvivorsPath || undefined,
      report
    });
    const queued = updateBatchQueueJob(jobId, {
      status: report.status || payload?.status || undefined,
      finishedAt: report.completedAt || undefined,
      outputPaths: Array.isArray(report.outputPaths) ? report.outputPaths : undefined,
      warnings: Array.isArray(report.warnings) ? report.warnings : undefined,
      errors: Array.isArray(report.errors) ? report.errors : undefined,
      importedTestId: report.importedTestId || undefined,
      importedAt: report.importedAt || undefined,
      importedAcceptedQuestionCount: Number.isFinite(report.importedAcceptedQuestionCount) ? report.importedAcceptedQuestionCount : undefined,
      acceptedSurvivorsImportedTestId: report.acceptedSurvivorsImportedTestId || undefined,
      acceptedSurvivorsImportedAt: report.acceptedSurvivorsImportedAt || undefined,
      decisionsPath: report.decisionsPath || undefined,
      acceptedSurvivorsPath: report.acceptedSurvivorsPath || undefined,
      report
    });
    if (!updated && !queued) return safeError('BATCH_JOB_NOT_FOUND', 'Batch job record was not found.');
    return { ok: true, job: updated || queued };
  } catch (err) {
    return safeError('BATCH_HISTORY_UPDATE_FAILED', err.message || String(err));
  }
});

ipcMain.handle('nbme:archive:write-quiz', async (_event, payload) => {
  try {
    const rawText = String(payload?.rawText || '');
    if (!rawText) return { ok: false, error: 'No JSON content to archive.' };
    function sanitizeSegment(value) {
      let s = String(value || '').trim();
      if (!s) return '';
      // Strip filesystem-unsafe chars and control chars
      s = s.replace(/[\/\\:*?"<>|\u0000-\u001F]/g, '_');
      // Collapse whitespace + repeated underscores
      s = s.replace(/\s+/g, '_').replace(/_+/g, '_');
      // Trim leading/trailing dots and underscores
      s = s.replace(/^[._]+|[._]+$/g, '');
      return s.slice(0, 120);
    }
    const sourceName = sanitizeSegment(payload?.sourceFolderName) || 'Unfiled';
    const subName = sanitizeSegment(payload?.subfolderName);
    const baseName = sanitizeSegment(payload?.testName) || 'untitled_quiz';
    const archiveRoot = path.join(app.getAppPath(), 'archive');
    const targetDir = subName
      ? path.join(archiveRoot, sourceName, subName)
      : path.join(archiveRoot, sourceName);
    fs.mkdirSync(targetDir, { recursive: true });
    // Collision handling — clean filename by default, suffix only on conflict
    let candidate = path.join(targetDir, baseName + '.json');
    let suffix = 2;
    while (fs.existsSync(candidate)) {
      candidate = path.join(targetDir, baseName + '_' + suffix + '.json');
      suffix += 1;
    }
    fs.writeFileSync(candidate, rawText, 'utf8');
    return {
      ok: true,
      path: candidate,
      relPath: path.relative(app.getAppPath(), candidate)
    };
  } catch (err) {
    return { ok: false, error: err && err.message ? err.message : String(err) };
  }
});

ipcMain.handle('nbme:batch-import:cancel-job', async (_event, payload) => {
  try {
    const jobId = String(payload?.jobId || '').trim();
    if (!jobId) return safeError('BATCH_JOB_ID_REQUIRED', 'jobId is required.');
    const active = activeBatchJobs.get(jobId);
    if (!active) {
      return safeError('BATCH_JOB_NOT_RUNNING', 'No running batch job was found for that id.');
    }
    // v4.78: idempotent cancel — pre-v4.78 each cancel click sent a fresh
    // SIGTERM, which showed up as 3x JOB_CANCELLED log lines for one user
    // action and burned CPU on signal handling.
    if (active.cancelled) {
      return { ok: true, jobId, status: 'canceled', alreadyCancelled: true };
    }
    active.cancelled = true;
    active.cancelledAt = new Date().toISOString();
    // v4.78: 'canceled' (one L) everywhere — pre-v4.78 this record used
    // 'cancelled' (two L's) while the queue + close-handler used 'canceled',
    // which left the record + queue out of sync.
    updateBatchJobRecord(jobId, {
      status: 'canceled',
      completedAt: active.cancelledAt,
      errors: [],
      warnings: [...(active.warnings || []), 'Job canceled by user.']
    });
    updateBatchQueueJob(jobId, {
      status: 'canceled',
      finishedAt: active.cancelledAt,
      warnings: [...(active.warnings || []), 'Job canceled by user.']
    });
    if (active.proc?.pid) {
      safeKillProcessGroup(active.proc, 'SIGTERM', `cancel ${jobId}`) || safeKillProcess(active.proc, 'SIGTERM', `cancel ${jobId}`);
      active.killTimer = setTimeout(() => {
        if (active.proc && active.proc.exitCode === null) {
          safeKillProcessGroup(active.proc, 'SIGKILL', `cancel fallback ${jobId}`);
        }
      }, 5000);
    } else {
      console.warn(`[NBME] Batch process kill skipped (cancel ${jobId}): pid missing.`);
    }
    return { ok: true, jobId, status: 'canceled' };
  } catch (err) {
    return safeError('BATCH_CANCEL_FAILED', err.message || String(err));
  }
});

// v4.65: pause/resume an active batch job via POSIX SIGSTOP / SIGCONT signals.
// The child is spawned with detached:true (process-group leader), so signaling
// -pid hits every grandchild (Gemini subprocesses, OCR helpers, etc.).
// SIGSTOP halts immediately — any in-flight Gemini HTTP call will complete at
// the OS socket layer but the Python event loop won't process it until SIGCONT.
ipcMain.handle('nbme:batch-import:pause-job', async (_event, payload) => {
  try {
    const jobId = String(payload?.jobId || '').trim();
    if (!jobId) return safeError('BATCH_JOB_ID_REQUIRED', 'jobId is required.');
    const active = activeBatchJobs.get(jobId);
    if (!active) return safeError('BATCH_JOB_NOT_RUNNING', 'No running batch job was found for that id.');
    if (active.paused) return { ok: true, jobId, status: 'paused', message: 'Already paused.' };
    if (!active.proc?.pid) return safeError('BATCH_JOB_NO_PID', 'No process pid to pause.');
    const sent = safeKillProcessGroup(active.proc, 'SIGSTOP', `pause ${jobId}`)
      || safeKillProcess(active.proc, 'SIGSTOP', `pause ${jobId}`);
    if (!sent) return safeError('BATCH_PAUSE_FAILED', 'Could not deliver SIGSTOP to the job process.');
    active.paused = true;
    active.pausedAt = new Date().toISOString();
    return { ok: true, jobId, status: 'paused', pausedAt: active.pausedAt };
  } catch (err) {
    return safeError('BATCH_PAUSE_FAILED', err.message || String(err));
  }
});

ipcMain.handle('nbme:batch-import:resume-job', async (_event, payload) => {
  try {
    const jobId = String(payload?.jobId || '').trim();
    if (!jobId) return safeError('BATCH_JOB_ID_REQUIRED', 'jobId is required.');
    const active = activeBatchJobs.get(jobId);
    if (!active) return safeError('BATCH_JOB_NOT_RUNNING', 'No running batch job was found for that id.');
    if (!active.paused) return { ok: true, jobId, status: 'running', message: 'Not paused.' };
    if (!active.proc?.pid) return safeError('BATCH_JOB_NO_PID', 'No process pid to resume.');
    const sent = safeKillProcessGroup(active.proc, 'SIGCONT', `resume ${jobId}`)
      || safeKillProcess(active.proc, 'SIGCONT', `resume ${jobId}`);
    if (!sent) return safeError('BATCH_RESUME_FAILED', 'Could not deliver SIGCONT to the job process.');
    active.paused = false;
    active.resumedAt = new Date().toISOString();
    return { ok: true, jobId, status: 'running', resumedAt: active.resumedAt };
  } catch (err) {
    return safeError('BATCH_RESUME_FAILED', err.message || String(err));
  }
});

ipcMain.handle('nbme:batch-import:retry-queue-job', async (_event, payload) => {
  try {
    const jobId = String(payload?.jobId || '').trim();
    const job = readBatchQueue().jobs.find(item => item.jobId === jobId);
    if (!job) return safeError('BATCH_JOB_NOT_FOUND', 'Queued batch job was not found.');
    if (!['failed', 'interrupted', 'canceled', 'needs_review', 'completed_with_review_required'].includes(job.status)) {
      return safeError('BATCH_JOB_NOT_RETRYABLE', 'Only failed, interrupted, canceled, or review-required jobs can be retried.');
    }
    const updated = updateBatchQueueJob(jobId, {
      status: 'pending',
      startedAt: null,
      finishedAt: null,
      progress: { phase: 'pending', message: 'Retry queued.', updatedAt: new Date().toISOString() },
      errors: []
    });
    scheduleBatchQueue();
    return { ok: true, job: updated };
  } catch (err) {
    return safeError('BATCH_RETRY_FAILED', err.message || String(err));
  }
});

// v5.6: byte-size lookup for the Advanced Mode cost preview. The
// renderer needs file sizes (post-picker) to project the question
// count + cost + wall time, but doesn't have Node fs access in this
// sandbox. Returns -1 on any error (missing file, permission denied,
// path traversal sanity-check, etc.) so the renderer can render a
// graceful fallback rather than throw.
ipcMain.handle('nbme:batch-import:file-size', async (_event, filePath) => {
  try {
    const resolved = path.resolve(String(filePath || '').trim());
    if (!resolved) return -1;
    const stat = await fs.promises.stat(resolved);
    if (!stat.isFile()) return -1;
    return stat.size;
  } catch (err) {
    return -1;
  }
});

ipcMain.handle('nbme:batch-import:select-files', async (_event, payload) => {
  try {
    const sourceType = String(payload?.sourceType || '').trim();
    if (!sourceType) return safeError('BATCH_SOURCE_REQUIRED', 'Choose a source type first.');
    const registry = readBatchRegistry();
    const source = registry.sources?.[sourceType];
    if (!source || source.status !== 'active') {
      return safeError('BATCH_SOURCE_UNKNOWN', `Source type is not registered: ${sourceType}`);
    }
    const existingOutputValidation = payload?.existingOutputValidation === true;
    const extensions = existingOutputValidation
      ? ['json']
      : Array.isArray(source.inputExtensions)
      ? source.inputExtensions.map(ext => String(ext).replace(/^\./, '').trim()).filter(Boolean)
      : [];
    const result = await dialog.showOpenDialog({
      title: existingOutputValidation ? 'Select existing app-ready JSON output' : `Select ${source.label || sourceType} file`,
      properties: source.allowDirectories && !existingOutputValidation
        ? ['openFile', 'openDirectory', 'multiSelections']
        : ['openFile', 'multiSelections'],
      filters: extensions.length
        ? [{ name: existingOutputValidation ? 'App-ready JSON' : (source.label || sourceType), extensions }, { name: 'All Files', extensions: ['*'] }]
        : [{ name: 'All Files', extensions: ['*'] }]
    });
    if (result.canceled) return { ok: true, canceled: true, filePaths: [] };
    return { ok: true, canceled: false, filePaths: result.filePaths || [] };
  } catch (err) {
    return safeError('BATCH_FILE_PICKER_FAILED', err.message || String(err));
  }
});

function enqueueBatchPayload(payload) {
  let manifest;
  let manifestPath;
  const sourceType = String(payload?.sourceType || '').trim();
  const registry = readBatchRegistry();
  const source = registry.sources?.[sourceType];
  if (!source || source.status !== 'active') {
    throw new Error(`Source type is not registered: ${sourceType}`);
  }
  manifest = sanitizeBatchJobPayload(payload, source);
  manifestPath = writeBatchManifest(manifest);
  persistBatchJobRecord(initialBatchJobRecord(manifest, source, manifestPath));
  return persistBatchQueueJob(queueJobFromManifest(manifest, source, payload, manifestPath));
}

ipcMain.handle('nbme:batch-import:enqueue-jobs', async (_event, payload) => {
  try {
    const payloads = Array.isArray(payload?.jobs) ? payload.jobs : [payload];
    const jobs = payloads.map(item => enqueueBatchPayload(item));
    scheduleBatchQueue();
    return { ok: true, jobs, queue: readBatchQueue() };
  } catch (err) {
    return safeError('BATCH_QUEUE_ENQUEUE_FAILED', err.message || String(err));
  }
});

ipcMain.handle('nbme:batch-import:launch-job', async (_event, payload) => {
  try {
    const job = enqueueBatchPayload(payload);
    const result = new Promise(resolve => batchQueueWaiters.set(job.jobId, resolve));
    scheduleBatchQueue();
    return await result;
  } catch (err) {
    return safeError('BATCH_JOB_INVALID', err.message || String(err));
  }
});

async function scheduleBatchQueue() {
  if (batchQueueRunning || activeBatchJobs.size) return;
  batchQueueRunning = true;
  try {
    while (!activeBatchJobs.size) {
      const next = readBatchQueue().jobs.find(job => job.status === 'pending');
      if (!next) break;
      await runQueuedBatchJob(next);
    }
  } finally {
    batchQueueRunning = false;
  }
}

async function runQueuedBatchJob(job) {
  let batchEnvironment;
  let manifest = job.manifest;
  const registry = readBatchRegistry();
  const source = registry.sources?.[job.sourceType];
  if (!source || !manifest) {
    settleBatchQueueWaiter(job.jobId, finishQueuedBatchFailure(job, 'Queued job is missing its source or manifest.'));
    return;
  }
  try {
    const dryRunRequiresGemini = !!source.dryRunRequiresGemini;
    if (manifest.requiresGemini && (!manifest.dryRun || dryRunRequiresGemini)) {
      batchEnvironment = await resolveBatchEnvironment(true);
      if (!batchEnvironment.ok) {
        settleBatchQueueWaiter(job.jobId, finishQueuedBatchFailure(job, batchEnvironment.message));
        return;
      }
    } else {
      batchEnvironment = await resolveBatchEnvironment(false);
    }
  } catch (err) {
    settleBatchQueueWaiter(job.jobId, finishQueuedBatchFailure(job, err.message || String(err)));
    return;
  }

  return await new Promise(resolve => {
    const manifestPath = job.manifestPath || writeBatchManifest(manifest);
    const warnings = [];
    const errors = [];
    const launchedAt = Date.now();
    updateBatchQueueJob(job.jobId, {
      status: 'running',
      startedAt: new Date().toISOString(),
      manifestPath,
      progress: { phase: 'preflight', message: 'Batch runner starting.', updatedAt: new Date().toISOString() }
    });
    updateBatchJobRecord(job.jobId, { status: 'running', startedAt: new Date().toISOString() });
    const deriveGraphProgress = graph => {
      if (!graph || typeof graph !== 'object' || !Array.isArray(graph.chunks)) return null;
      const totalChunks = Number(graph.totalChunks || graph.chunks.length || 0);
      if (!totalChunks) return null;
      const completedChunks = graph.chunks.filter(chunk => chunk && chunk.state === 'completed').length;
      const active = graph.chunks.find(chunk => chunk && (chunk.state === 'running' || chunk.state === 'retrying'))
        || graph.chunks.find(chunk => chunk && chunk.state === 'planned')
        || null;
      const attempts = active && Array.isArray(active.attempts) ? active.attempts : [];
      const latestAttempt = attempts.length ? attempts[attempts.length - 1] : null;
      return {
        event: 'EXECUTION_GRAPH',
        totalChunks,
        totalQuestions: 0,
        chunkAllocation: graph.chunks.map(chunk => Number(chunk.expectedQuestions || 0)),
        completedChunks,
        activeChunk: active ? String(active.chunkId || '') : '',
        chunkIndex: active ? Number(active.index || 0) : 0,
        currentPhase: active ? String(active.state || '') : '',
        retryState: latestAttempt ? {
          attemptNumber: String(latestAttempt.phase || '') === 'repair' ? 1 : 0,
          maxRepairAttempts: 1,
          globalRetryId: Number(latestAttempt.attemptId || 0),
          retryPhase: latestAttempt.phase || '',
          status: latestAttempt.status || '',
          reason: latestAttempt.error || ''
        } : null,
        heartbeatLastSeen: '',
        elapsedMs: 0,
        lastEvent: 'EXECUTION_GRAPH',
        executionGraph: graph
      };
    };
    let organicJobComplete = false;
    const sendProgress = eventPayload => {
      const enriched = { jobId: manifest.jobId, ...eventPayload };
      appendBatchLog(job.logsPath, enriched);
      const chunkProgress = deriveGraphProgress(enriched.executionGraph);
      if (chunkProgress && enriched.chunkEvent === 'CHUNK_HEARTBEAT') {
        chunkProgress.heartbeatLastSeen = enriched.timestamp || new Date().toISOString();
        chunkProgress.elapsedMs = Number(enriched.elapsedMs || 0);
        chunkProgress.lastEvent = enriched.chunkEvent;
      }
      if (chunkProgress && (enriched.event === 'JOB_COMPLETE' || enriched.chunkEvent === 'JOB_COMPLETE')) {
        organicJobComplete = true;
        chunkProgress.completedChunks = chunkProgress.totalChunks;
        chunkProgress.chunkIndex = chunkProgress.totalChunks;
        chunkProgress.currentPhase = 'finalizing';
        chunkProgress.activeChunk = '';
        chunkProgress.retryState = null;
        chunkProgress.lastEvent = 'JOB_COMPLETE';
      }
      if (enriched.type === 'stage_start' && enriched.stage) {
        updateBatchJobRecord(manifest.jobId, { currentStage: enriched.stage });
      }
      if (enriched.type === 'stage_start' || enriched.type === 'pipeline_progress' || enriched.type === 'stage_heartbeat' || enriched.type === 'log') {
        const organicComplete = enriched.event === 'JOB_COMPLETE' || enriched.chunkEvent === 'JOB_COMPLETE';
        const updatedAt = enriched.timestamp || new Date().toISOString();
        updateBatchQueueJob(manifest.jobId, {
          progress: {
            phase: organicComplete ? 'finalizing' : (enriched.stage || enriched.phase || enriched.stageLabel || enriched.type),
            message: organicComplete ? 'Generation finished. Preparing app-ready output.' : (enriched.message || enriched.stageLabel || enriched.type),
            updatedAt,
            ...(chunkProgress ? { chunk: chunkProgress } : {})
          },
          ...(chunkProgress ? { chunkProgress } : {}),
          lastHeartbeatAt: updatedAt
        });
      }
      if (enriched.type === 'warning' || enriched.type === 'cache_summary') {
        const message = enriched.message || enriched.warning;
        if (message) warnings.push(String(message));
      }
      if (enriched.type === 'error' || enriched.type === 'stage_failed') {
        const message = enriched.message || enriched.error || enriched.failureReason;
        if (message) errors.push(String(message));
      }
      BrowserWindow.getAllWindows().forEach(win => {
        if (!win.isDestroyed()) win.webContents.send('nbme:batch-import:progress', enriched);
      });
    };

    // v4.75: route pipeline outputs to the BIC's per-job userData directory
    // (~/Library/Application Support/<appName>/batch-import-center/jobs/<jobId>/).
    // Without these env vars, each downstream generator falls back to writing
    // INSIDE the .app bundle (e.g. shamsulalamx.app/Contents/Resources/app/
    // tools/mehlman-pdf-question-generator/output_json/app_ready/). That is
    // wrong for two reasons: (1) .app bundles aren't valid macOS storage,
    // and (2) every `npm run electron:build:mac` overwrites the bundle's
    // contents, silently destroying any previous pipeline outputs. Every
    // *_profile_runner.py in tools/shared-ingestion/ already checks
    // BIC_JOB_OUTPUT_ROOT and routes outputs there if set — we just weren't
    // setting it. This commit fixes that.
    const baseChildEnv = batchEnvironment?.env || process.env;
    const childEnv = Object.assign({}, baseChildEnv, {
      BIC_JOB_OUTPUT_ROOT: manifest.outputRoot || '',
      BIC_JOB_ID: manifest.jobId || '',
      BIC_PROGRESS_SOURCE: manifest.sourceType || ''
    });
    const useLoginShell = !!batchEnvironment?.useLoginShell;
    const command = useLoginShell ? '/bin/zsh' : 'python3';
    const args = useLoginShell
      ? ['-lc', 'source ~/.zshrc >/dev/null 2>&1 || true; exec "$@"', 'nbme-batch-python', 'python3', BATCH_IMPORT_RUNNER, manifestPath]
      : [BATCH_IMPORT_RUNNER, manifestPath];
    const proc = spawn(command, args, {
      cwd: app.getAppPath(),
      env: childEnv,
      stdio: ['ignore', 'pipe', 'pipe'],
      detached: true
    });
    activeBatchJobs.set(manifest.jobId, { proc, manifest, manifestPath, warnings, errors, cancelled: false, killTimer: null });
    writeBatchProcessRegistry(job, proc.pid);

    let stdoutBuffer = '';
    let stderrBuffer = '';
    let finalEvent = null;
    function consumeLine(line, streamName) {
      const trimmed = line.trim();
      if (!trimmed) return;
      if (trimmed.startsWith('BIC_PROGRESS ')) return;
      try {
        const parsed = JSON.parse(trimmed);
        if (parsed.type === 'job_complete') finalEvent = parsed;
        sendProgress(parsed);
      } catch (_) {
        sendProgress({ type: 'log', stream: streamName, message: trimmed, timestamp: new Date().toISOString() });
      }
    }
    proc.stdout.on('data', chunk => {
      stdoutBuffer += chunk.toString();
      const lines = stdoutBuffer.split(/\r?\n/);
      stdoutBuffer = lines.pop() || '';
      lines.forEach(line => consumeLine(line, 'stdout'));
    });
    proc.stderr.on('data', chunk => {
      stderrBuffer += chunk.toString();
      const lines = stderrBuffer.split(/\r?\n/);
      stderrBuffer = lines.pop() || '';
      lines.forEach(line => consumeLine(line, 'stderr'));
    });
    proc.on('error', err => {
      activeBatchJobs.delete(manifest.jobId);
      const result = finishQueuedBatchFailure(job, err.message || String(err), launchedAt);
      settleBatchQueueWaiter(job.jobId, result);
      resolve();
    });
    proc.on('close', (code, signal) => {
      consumeLine(stdoutBuffer, 'stdout');
      consumeLine(stderrBuffer, 'stderr');
      const active = activeBatchJobs.get(manifest.jobId);
      if (active?.killTimer) clearTimeout(active.killTimer);
      activeBatchJobs.delete(manifest.jobId);
      // v4.78: also trust the Python runner's emitted `cancelled` flag — the
      // in-memory active.cancelled can miss races (e.g. external SIGTERM, OS-level
      // kill, or cancel handler that returned NOT_RUNNING right when proc died).
      // Symptom pre-v4.78: completion-report.json saved with status='failed' and
      // errors=['Job cancelled.'] instead of status='canceled' with no errors.
      const wasCancelled = !!active?.cancelled || (finalEvent && finalEvent.cancelled === true);
      const runtimeSeconds = Math.round((Date.now() - launchedAt) / 100) / 10;
      const ok = !wasCancelled && code === 0 && (!finalEvent || finalEvent.ok !== false);
      const report = finalEvent?.report && typeof finalEvent.report === 'object' ? finalEvent.report : {};
      const outputPaths = Array.isArray(finalEvent?.outputs) ? finalEvent.outputs : [];
      const fallbackMessage = signal ? `Batch job exited with signal ${signal}.` : `Batch job exited with code ${code}.`;
      const reportWarnings = Array.isArray(report.warnings) ? report.warnings.map(String) : [];
      const reportErrors = Array.isArray(report.errors) ? report.errors.map(String) : [];
      const finalWarnings = [...warnings, ...reportWarnings];
      const recovery = report.recovery && typeof report.recovery === 'object' ? report.recovery : null;
      const recoveryOutcome = String(recovery?.outcome || '');
      const reportFatal = recoveryOutcome === 'failed_fatal';
      const finalErrors = wasCancelled ? [] : (errors.length ? errors : (ok && !reportFatal ? reportErrors : (reportErrors.length ? reportErrors : [finalEvent?.error || fallbackMessage])));
      const reviewRequiredStatus = report.status === 'needs_review' || report.status === 'completed_with_review_required';
      const status = wasCancelled ? 'canceled' : (ok && !reportFatal ? (reviewRequiredStatus ? 'completed_with_review_required' : (report.status || 'completed')) : 'failed');
      const finalReport = { ...report, status, runtimeSeconds, outputPaths, warnings: finalWarnings, errors: finalErrors };
      if (ok && !wasCancelled && !reportFatal && job.runMode === 'generate-auto-import' && !reviewRequiredStatus && !outputPaths.length) {
        finalReport.status = 'failed';
        finalReport.stage = 'failed';
        finalReport.errors = [...finalErrors, 'Generation finished but no app-ready output was discovered for auto-import.'];
      }
      const reportPath = writeBatchCompletionReport(job.outputRoot, finalReport);
      const finishedAt = new Date().toISOString();
      const finalStatus = finalReport.status || status;
      const finalOk = ok && finalStatus !== 'failed';
      const finalErrorList = Array.isArray(finalReport.errors) ? finalReport.errors : [];
      updateBatchJobRecord(manifest.jobId, {
        status: finalStatus,
        completedAt: finishedAt,
        runtimeSeconds,
        currentStage: finalStatus === 'completed' || finalStatus === 'completed_with_review_required' ? finalStatus : report.stage || null,
        outputPaths,
        draftPath: report.draftPath || '',
        reportPath,
        warnings: wasCancelled ? [...finalWarnings, 'Job canceled by user.'] : finalWarnings,
        errors: finalErrorList,
        recovery,
        report: finalReport
      });
      updateBatchQueueJob(manifest.jobId, {
        status: finalStatus,
        finishedAt,
        outputPaths,
        draftPath: report.draftPath || '',
        reportPath,
        warnings: wasCancelled ? [...finalWarnings, 'Job canceled by user.'] : finalWarnings,
        errors: finalErrorList,
        recovery,
        report: finalReport,
        progress: {
          phase: finalStatus,
          message: finalStatus === 'completed'
            ? 'Batch job completed.'
            : (finalStatus === 'completed_with_review_required' ? 'Batch job completed with review artifacts.' : ((finalReport.errors || [])[0] || finalStatus)),
          updatedAt: finishedAt
        }
      });
      updateBatchProcessRegistryStatus(job, finalStatus);
      const result = {
        ok: finalOk,
        cancelled: wasCancelled,
        exitCode: code,
        signal,
        manifestPath,
        manifest,
        outputs: outputPaths,
        progress: [],
        report: finalReport,
        errorCode: finalOk ? null : (wasCancelled ? 'BATCH_JOB_CANCELLED' : 'BATCH_JOB_FAILED'),
        message: finalOk ? null : (wasCancelled ? 'Batch job canceled.' : (finalErrorList[0] || finalEvent?.error || fallbackMessage))
      };
      settleBatchQueueWaiter(job.jobId, result);
      resolve();
    });
  });
}

function finishQueuedBatchFailure(job, message, launchedAt = Date.now()) {
  const finishedAt = new Date().toISOString();
  const errors = [String(message || 'Queued batch job failed.')];
  updateBatchJobRecord(job.jobId, {
    status: 'failed',
    completedAt: finishedAt,
    runtimeSeconds: Math.round((Date.now() - launchedAt) / 100) / 10,
    errors
  });
  updateBatchQueueJob(job.jobId, {
    status: 'failed',
    finishedAt,
    errors,
    progress: { phase: 'failed', message: errors[0], updatedAt: finishedAt }
  });
  return safeError('BATCH_JOB_FAILED', errors[0]);
}

function settleBatchQueueWaiter(jobId, result) {
  const waiter = batchQueueWaiters.get(jobId);
  if (!waiter) return;
  batchQueueWaiters.delete(jobId);
  waiter(result);
}

ipcMain.handle('nbme:batch-import:read-output-json', async (_event, outputPath) => {
  try {
    const resolved = path.resolve(String(outputPath || ''));
    if (!resolved.endsWith('_app_ready.json')) {
      return safeError('BATCH_OUTPUT_REJECTED', 'Only *_app_ready.json outputs can be read for auto-import.');
    }
    if (!fs.existsSync(resolved) || !fs.statSync(resolved).isFile()) {
      return safeError('BATCH_OUTPUT_MISSING', 'Output file was not found.');
    }
    return {
      ok: true,
      path: resolved,
      text: fs.readFileSync(resolved, 'utf8')
    };
  } catch (err) {
    return safeError('BATCH_OUTPUT_READ_FAILED', err.message || String(err));
  }
});

function safeError(errorCode, message) {
  return { ok: false, errorCode, message };
}

function clampText(value, maxLength) {
  return String(value || '').replace(/\s+/g, ' ').trim().slice(0, maxLength);
}

function normalizeStringArray(value, maxItems = 12) {
  if (!Array.isArray(value)) return [];
  return value.map(v => clampText(v, 160)).filter(Boolean).slice(0, maxItems);
}

function sanitizeDraftInput(payload) {
  if (!payload || typeof payload !== 'object') return null;
  const draft = payload.draft && typeof payload.draft === 'object' ? payload.draft : null;
  const concept = payload.concept && typeof payload.concept === 'object' ? payload.concept : null;
  if (!draft || !concept) return null;

  const sourceBlockIds = normalizeStringArray(draft.sourceBlockIds || concept.sourceBlockIds, 20);
  return {
    draft: {
      draftId: clampText(draft.draftId, 80),
      stem: clampText(draft.stem, 1600),
      choices: Array.isArray(draft.choices) ? draft.choices.slice(0, 5).map(choice => ({
        label: clampText(choice.label || choice.l, 2),
        text: clampText(choice.text || choice.t, 320)
      })) : [],
      correctAnswer: clampText(draft.correctAnswer, 2),
      teachingPoint: clampText(draft.teachingPoint, 700),
      warnings: normalizeStringArray(draft.warnings, 12)
    },
    concept: {
      conceptId: clampText(concept.conceptId || draft.sourceConceptId || draft.conceptId, 80),
      topic: clampText(concept.topic, 220),
      testedFact: clampText(concept.testedFact, 1600),
      sourceSnippet: clampText(concept.sourceSnippet || payload.sourceSnippet, 1200),
      confidence: Number.isFinite(concept.confidence) ? concept.confidence : null,
      warnings: normalizeStringArray(concept.warnings, 12)
    },
    sourceMeta: {
      sourceName: clampText(payload.sourceMeta?.sourceName, 220),
      sourceHash: clampText(payload.sourceMeta?.sourceHash, 96)
    },
    sourceBlockIds
  };
}

function buildRefinementPrompt(input) {
  return [
    'You refine deterministic UWorld notes question scaffolds into Step 2/NBME-style multiple choice question drafts.',
    'Return strict JSON only. Do not include markdown fences or explanatory text outside JSON.',
    'Use only the supplied source note and concept. Do not invent unsupported facts. Do not copy commercial question-bank wording.',
    'If the source is insufficient, still return the JSON schema, set needsReview true, lower confidence, and explain the limitation in warnings.',
    'Produce exactly five answer choices labeled A, B, C, D, and E. Include one correct answer and concise rationales for all choices.',
    'This output is preview-only and requires review before use.',
    '',
    'Required JSON schema:',
    JSON.stringify({
      stem: 'string',
      choices: [
        { label: 'A', text: 'string' },
        { label: 'B', text: 'string' },
        { label: 'C', text: 'string' },
        { label: 'D', text: 'string' },
        { label: 'E', text: 'string' }
      ],
      correctAnswer: 'A',
      teachingPoint: 'string',
      rationales: { A: 'string', B: 'string', C: 'string', D: 'string', E: 'string' },
      confidence: 0.5,
      needsReview: true,
      warnings: ['string']
    }),
    '',
    'Input:',
    JSON.stringify(input)
  ].join('\n');
}

function extractGeminiJson(data) {
  const text = data?.candidates?.[0]?.content?.parts
    ?.map(part => part.text || '')
    .join('')
    .trim();
  if (!text) throw new SyntaxError('empty model response');

  // Attempt 1: strip leading/trailing markdown fences and parse directly.
  const fenceStripped = text
    .replace(/^```(?:json)?\s*/i, '')
    .replace(/\s*```$/i, '')
    .trim();
  try {
    return JSON.parse(fenceStripped);
  } catch (_) {}

  // Attempt 2: locate the first top-level JSON object by brace scanning.
  // Tracks string/escape state so inner braces inside string values are ignored.
  const start = text.indexOf('{');
  if (start !== -1) {
    let depth = 0;
    let inString = false;
    let escape = false;
    for (let i = start; i < text.length; i++) {
      const ch = text[i];
      if (escape) { escape = false; continue; }
      if (ch === '\\' && inString) { escape = true; continue; }
      if (ch === '"') { inString = !inString; continue; }
      if (inString) continue;
      if (ch === '{') depth++;
      else if (ch === '}') {
        depth--;
        if (depth === 0) {
          return JSON.parse(text.slice(start, i + 1));
        }
      }
    }
  }

  throw new SyntaxError('no valid JSON object found in model response');
}

function validateRefinedDraft(raw, input) {
  if (!raw || typeof raw !== 'object') throw new Error('response is not an object');
  const labels = ['A', 'B', 'C', 'D', 'E'];
  const choices = Array.isArray(raw.choices) ? raw.choices : [];
  if (choices.length !== 5) throw new Error('expected exactly five choices');

  const normalizedChoices = choices.map((choice, idx) => {
    const label = clampText(choice?.label, 2).toUpperCase();
    const text = clampText(choice?.text, 600);
    if (label !== labels[idx]) throw new Error('choice labels must be A through E in order');
    if (!text) throw new Error('choice text cannot be empty');
    return { label, text };
  });

  const stem = clampText(raw.stem, 2800);
  const teachingPoint = clampText(raw.teachingPoint, 1000);
  const correctAnswer = clampText(raw.correctAnswer, 2).toUpperCase();
  const rationales = raw.rationales && typeof raw.rationales === 'object' ? raw.rationales : {};
  if (stem.length < 20) throw new Error('stem is too short');
  if (teachingPoint.length < 8) throw new Error('teaching point is too short');
  if (!labels.includes(correctAnswer)) throw new Error('correct answer must be A through E');

  const normalizedRationales = {};
  labels.forEach(label => {
    const rationale = clampText(rationales[label], 700);
    if (!rationale) throw new Error(`missing rationale ${label}`);
    normalizedRationales[label] = rationale;
  });

  const warnings = normalizeStringArray(raw.warnings, 12);
  if (!warnings.includes('requires review before use')) warnings.push('requires review before use');

  return {
    refinedDraftId: `refined-${input.draft.draftId || Date.now().toString(36)}`,
    sourceDraftId: input.draft.draftId,
    sourceConceptId: input.concept.conceptId,
    sourceBlockIds: input.sourceBlockIds.slice(),
    sourceName: input.sourceMeta.sourceName,
    sourceHash: input.sourceMeta.sourceHash,
    stem,
    choices: normalizedChoices,
    correctAnswer,
    teachingPoint,
    rationales: normalizedRationales,
    confidence: Math.max(0, Math.min(1, Number.isFinite(raw.confidence) ? raw.confidence : 0.35)),
    needsReview: raw.needsReview !== false || warnings.length > 0,
    warnings,
    model: GEMINI_MODEL,
    generationMethod: 'electron-gemini-uworld-draft-refinement-v1',
    createdAt: new Date().toISOString()
  };
}

ipcMain.handle('nbme:ai:refine-uworld-draft', async (_event, payload) => {
  const apiKey = ((payload?.apiKey || '').trim()) || process.env.GEMINI_API_KEY || '';
  if (!apiKey) return safeError('NO_API_KEY', 'Gemini API key is not configured. Enter it in Settings.');

  const input = sanitizeDraftInput(payload);
  if (!input || !input.draft.draftId || !input.concept.conceptId) {
    return safeError('MODEL_RESPONSE_INVALID', 'Draft refinement input is incomplete.');
  }

  const controller = new AbortController();
  const timeout = setTimeout(() => controller.abort(), 30000);

  try {
    const response = await fetch(GEMINI_ENDPOINT, {
      method: 'POST',
      signal: controller.signal,
      headers: {
        'Content-Type': 'application/json',
        'x-goog-api-key': apiKey
      },
      body: JSON.stringify({
        contents: [{ role: 'user', parts: [{ text: buildRefinementPrompt(input) }] }],
        generationConfig: {
          responseMimeType: 'application/json',
          temperature: 0.25,
          maxOutputTokens: 2200
        }
      })
    });

    if (response.status === 429) return safeError('RATE_LIMITED', 'Gemini rate limit reached. Try again later.');
    if (!response.ok) return safeError('NETWORK_ERROR', 'Gemini request failed before a valid response was returned.');

    const data = await response.json();

    let parsed;
    try {
      parsed = extractGeminiJson(data);
    } catch (parseErr) {
      return safeError('MODEL_RESPONSE_INVALID', `Gemini response could not be parsed as JSON: ${parseErr.message}`);
    }

    let refinedDraft;
    try {
      refinedDraft = validateRefinedDraft(parsed, input);
    } catch (schemaErr) {
      return safeError('MODEL_RESPONSE_INVALID', `Gemini response failed schema validation: ${schemaErr.message}`);
    }

    return { ok: true, refinedDraft };
  } catch (err) {
    if (err?.name === 'AbortError') return safeError('TIMEOUT', 'Gemini request timed out.');
    if (err instanceof TypeError) return safeError('NETWORK_ERROR', 'Gemini request failed because the network request could not be completed.');
    return safeError('MODEL_RESPONSE_INVALID', 'Gemini response handling failed unexpectedly.');
  } finally {
    clearTimeout(timeout);
  }
});

// ── Divine draft refinement — Gemini IPC ─────────────────────────────────────
// Refines a teaching cluster into a Step 2/NBME-style clinical vignette question.
// Gemini identifies the testable medical fact from clusterSummary.
// The app sends cleaned teaching clusters only; Gemini extracts the medical fact itself.
// sourceContext is capped at 300 chars and used only for coherence/copy detection.

// Podcast/coaching register patterns that must not appear in Gemini-generated stems.
// Reproduced here (cannot import from renderer) — kept in sync with renderer DIVINE_VOICE_MARKERS.
const DIVINE_STEM_VOICE_MARKERS = [
  /\byou need to\b/i,
  /\bI think\b/i,
  /\bremember\b/i,
  /\bdon'?t forget\b/i,
  /\bhigh[\s-]yield\b/i,
  /\bboards?\b/i,
  /\bpodcast\b/i,
  /\bI want you to\b/i,
  /\bthey give you\b/i
];

// Sanitize and clamp all renderer-supplied fields before they enter the prompt.
// Returns null on any structural failure — caller must treat null as invalid input.
function sanitizeDivineDraftInput(payload) {
  if (!payload || typeof payload !== 'object') return null;

  // clusterSummary is the primary medical source — required, trimmed, clamped ≤400 chars.
  const clusterSummary = clampText(payload.clusterSummary, 400);
  if (clusterSummary.length < 20) return null;

  const conceptType = clampText(payload.conceptType, 80);

  // variantType is optional; accepted as-is if present.
  const variantType = (payload.variantType != null) ? clampText(payload.variantType, 60) : null;

  // sourceContext is optional, hard-capped at 300 chars (coherence/copy detection only).
  const sourceContext = clampText(payload.sourceContext, 300);

  // sourceMeta — draftId and clusterId are required for provenance construction.
  const meta = (payload.sourceMeta && typeof payload.sourceMeta === 'object') ? payload.sourceMeta : {};
  const sourceMeta = {
    draftId:    clampText(meta.draftId,    80),
    clusterId:  clampText(meta.clusterId,  80),
    sourceName: clampText(meta.sourceName, 220),
    sourceHash: clampText(meta.sourceHash,  96)
  };
  if (!sourceMeta.draftId || !sourceMeta.clusterId) return null;

  // provenance — build from renderer-supplied fields; none are trusted blindly.
  const prov = (payload.provenance && typeof payload.provenance === 'object') ? payload.provenance : {};
  const tsRaw = prov.timestampRange;
  const provenance = {
    sourceSegmentIds:   normalizeStringArray(prov.sourceSegmentIds, 12),
    originalLineRanges: Array.isArray(prov.originalLineRanges) ? prov.originalLineRanges.slice(0, 12) : [],
    cleanedLineRanges:  Array.isArray(prov.cleanedLineRanges)  ? prov.cleanedLineRanges.slice(0, 12)  : [],
    timestampRanges:    Array.isArray(prov.timestampRanges)    ? prov.timestampRanges.slice(0, 12)    : [],
    timestampRange:     (tsRaw && typeof tsRaw === 'object')
                        ? { start: clampText(tsRaw.start, 20), end: clampText(tsRaw.end, 20) }
                        : null
  };

  return { conceptType, clusterSummary, sourceContext, variantType, sourceMeta, provenance };
}

// Build the Gemini prompt. clusterSummary is the sole medical source.
// Gemini extracts the testable fact itself — no hardcoded diagnostic criteria.
// sourceContext is appended last, labelled "do not copy", for coherence verification only.
function buildDivineRefinementPrompt(input) {
  const lines = [
    'You are a medical education question writer. Generate a Step 2/NBME-style clinical vignette multiple-choice question.',
    'Return strict JSON only. Do not include markdown fences or explanatory text outside JSON.',
    '',
    'CRITICAL RULES — follow exactly:',
    '1. PRIMARY INPUT is the clusterSummary below. First identify the single most testable medical fact it contains.',
    '   Record that fact as extractedTestableFact in your response.',
    '2. Choose the most accurate questionType from this exact list:',
    '   timeline-criterion | diagnostic-distinction | mechanism | management |',
    '   risk-factor | contraindication | clinical-application | other',
    '3. sourceContext is provenance only — do NOT copy, paraphrase, or echo any of its wording into the question.',
    '4. Forbidden podcast/coaching language in stem and choices:',
    '   remember, high yield, boards, you need to know, they give you,',
    '   I think, I want you to, don\'t forget, podcast voice.',
    '5. Write a concise but realistic clinical vignette: patient demographics, presenting symptoms,',
    '   relevant history, labs or imaging if needed, then the question.',
    '6. Generate exactly five answer choices labeled A through E. One best answer only.',
    '7. Choices must be clinically plausible, mutually exclusive, and not lifted from sourceContext.',
    '8. No transcript phrasing reuse. No coaching language. No "all of the above".',
    '9. If source material is insufficient, still produce all five choices, set needsReview true,',
    '   lower confidence, and explain in warnings.',
    '10. This output is preview-only and requires expert review before clinical use.',
    '',
    'Required JSON schema (return exactly this structure):',
    JSON.stringify({
      extractedTestableFact: 'string — the specific medical fact identified as testable',
      questionType: 'timeline-criterion | diagnostic-distinction | mechanism | management | risk-factor | contraindication | clinical-application | other',
      stem: 'string',
      choices: [
        { label: 'A', text: 'string' },
        { label: 'B', text: 'string' },
        { label: 'C', text: 'string' },
        { label: 'D', text: 'string' },
        { label: 'E', text: 'string' }
      ],
      correctAnswer: 'A',
      teachingPoint: 'string',
      rationales: { A: 'string', B: 'string', C: 'string', D: 'string', E: 'string' },
      confidence: 0.85,
      needsReview: false,
      warnings: []
    }),
    ''
  ];

  if (input.conceptType) lines.push(`Concept type: ${input.conceptType}`);
  if (input.variantType) lines.push(`Variant hint: ${input.variantType}`);

  lines.push(
    '',
    'Teaching cluster (primary medical input — extract the testable fact from this):',
    input.clusterSummary,
    '',
    'Source context — provenance only, do NOT copy or echo any phrasing:',
    input.sourceContext || '(none)'
  );

  return lines.join('\n');
}

// Detect verbatim overlap between Gemini output and the source context.
// Returns true if any 8-consecutive-word sequence from sourceContext appears in text.
// Prevents Gemini from lifting podcast transcript phrasing into the question stem or choices.
function divineCopyOverlapDetected(text, sourceContext) {
  if (!sourceContext || !text) return false;
  const srcWords  = sourceContext.toLowerCase().split(/\s+/).filter(Boolean);
  const testWords = text.toLowerCase().split(/\s+/).filter(Boolean);
  if (srcWords.length < 8 || testWords.length < 8) return false;
  const testStr = testWords.join(' ');
  const WINDOW  = 8;
  for (let i = 0; i <= srcWords.length - WINDOW; i++) {
    const ngram = srcWords.slice(i, i + WINDOW).join(' ');
    if (testStr.includes(ngram)) return true;
  }
  return false;
}

// Validate and normalize Gemini's raw JSON object.
// All provenance fields are constructed from sanitized input — nothing is trusted from Gemini.
// Throws with a specific human-readable message on any failure.
function validateDivineRefinedDraft(raw, input) {
  if (!raw || typeof raw !== 'object') throw new Error('response is not an object');

  const labels = ['A', 'B', 'C', 'D', 'E'];

  // 1. extractedTestableFact — Gemini-identified testable fact; must be substantive.
  const extractedTestableFact = clampText(raw.extractedTestableFact, 600).trim();
  if (extractedTestableFact.length < 10) {
    throw new Error(`extractedTestableFact too short (${extractedTestableFact.length} chars, need ≥10)`);
  }

  // 2. questionType — must be nonempty; clamped to 80 chars.
  const questionType = clampText(raw.questionType, 80);
  if (!questionType) throw new Error('questionType is missing or empty');

  // 3. stem — clinical vignette; must be substantive.
  const stem = clampText(raw.stem, 3000);
  if (stem.length < 40) throw new Error(`stem too short (${stem.length} chars, need ≥40)`);

  // 4-5. choices — exactly 5, labels A through E in order.
  const rawChoices = Array.isArray(raw.choices) ? raw.choices : [];
  if (rawChoices.length !== 5) throw new Error(`expected exactly 5 choices, got ${rawChoices.length}`);
  const normalizedChoices = rawChoices.map((choice, idx) => {
    const label = clampText(choice?.label, 2).toUpperCase();
    const text  = clampText(choice?.text, 600);
    if (label !== labels[idx]) throw new Error(`choice label must be ${labels[idx]}, got "${label}"`);
    if (!text) throw new Error(`choice text is empty for label ${labels[idx]}`);
    return { label, text };
  });

  // 6. correctAnswer — must be one of A–E.
  const correctAnswer = clampText(raw.correctAnswer, 2).toUpperCase();
  if (!labels.includes(correctAnswer)) throw new Error('correctAnswer must be A through E');

  // 7. teachingPoint — must be a substantive clinical statement.
  const teachingPoint = clampText(raw.teachingPoint, 1200);
  if (teachingPoint.length < 20) throw new Error(`teachingPoint too short (${teachingPoint.length} chars, need ≥20)`);

  // 8. rationales — all five labels required and nonempty.
  const rawRationales = (raw.rationales && typeof raw.rationales === 'object') ? raw.rationales : {};
  const normalizedRationales = {};
  for (const label of labels) {
    const rationale = clampText(rawRationales[label], 700);
    if (!rationale) throw new Error(`missing rationale for choice ${label}`);
    normalizedRationales[label] = rationale;
  }

  // 9. anti-copy: no 8-word verbatim overlap between sourceContext and stem or any choice.
  const sourceContext = input.sourceContext || '';
  if (divineCopyOverlapDetected(stem, sourceContext)) {
    throw new Error('stem contains verbatim overlap with source context (≥8 consecutive words)');
  }
  for (const { label, text } of normalizedChoices) {
    if (divineCopyOverlapDetected(text, sourceContext)) {
      throw new Error(`choice ${label} contains verbatim overlap with source context (≥8 consecutive words)`);
    }
  }

  // 10. no podcast/coaching voice in the stem.
  for (const marker of DIVINE_STEM_VOICE_MARKERS) {
    if (marker.test(stem)) {
      throw new Error(`stem contains podcast/coaching language matching /${marker.source}/`);
    }
  }

  // 11. warnings — normalise; always append the review sentinel.
  const warnings = normalizeStringArray(raw.warnings, 12);
  if (!warnings.includes('requires review before use')) warnings.push('requires review before use');

  // 12. Assemble result. All provenance comes from sanitized input — never from Gemini output.
  return {
    extractedTestableFact,
    questionType,
    refinedDraftId: `divine-refined-${input.sourceMeta.draftId}-${Date.now().toString(36)}`,
    draftId:        input.sourceMeta.draftId,
    clusterId:      input.sourceMeta.clusterId,
    sourceName:     input.sourceMeta.sourceName,
    sourceHash:     input.sourceMeta.sourceHash,
    provenance:     input.provenance,
    stem,
    choices:        normalizedChoices,
    correctAnswer,
    teachingPoint,
    rationales:     normalizedRationales,
    confidence:     Math.max(0, Math.min(1, Number.isFinite(raw.confidence) ? raw.confidence : 0.35)),
    needsReview:    raw.needsReview !== false || warnings.length > 1,
    warnings,
    model:             GEMINI_MODEL,
    generationMethod:  'electron-gemini-divine-cluster-v2',
    createdAt:         new Date().toISOString()
  };
}

ipcMain.handle('nbme:ai:refine-divine-draft', async (_event, payload) => {
  const apiKey = ((payload?.apiKey || '').trim()) || process.env.GEMINI_API_KEY || '';
  if (!apiKey) return safeError('NO_API_KEY', 'Gemini API key is not configured. Enter it in Settings.');

  const input = sanitizeDivineDraftInput(payload);
  if (!input) {
    return safeError('MODEL_RESPONSE_INVALID', 'Divine draft refinement input is incomplete or malformed.');
  }

  const controller = new AbortController();
  const timeout = setTimeout(() => controller.abort(), 30000);

  try {
    const response = await fetch(GEMINI_ENDPOINT, {
      method: 'POST',
      signal: controller.signal,
      headers: {
        'Content-Type': 'application/json',
        'x-goog-api-key': apiKey
      },
      body: JSON.stringify({
        contents: [{ role: 'user', parts: [{ text: buildDivineRefinementPrompt(input) }] }],
        generationConfig: {
          responseMimeType: 'application/json',
          temperature: 0.30,
          maxOutputTokens: 2400
        }
      })
    });

    if (response.status === 429) return safeError('RATE_LIMITED', 'Gemini rate limit reached. Try again later.');
    if (!response.ok) return safeError('NETWORK_ERROR', 'Gemini request failed before a valid response was returned.');

    const data = await response.json();

    let parsed;
    try {
      parsed = extractGeminiJson(data);
    } catch (parseErr) {
      return safeError('MODEL_RESPONSE_INVALID', `Gemini response could not be parsed as JSON: ${parseErr.message}`);
    }

    let refinedDraft;
    try {
      refinedDraft = validateDivineRefinedDraft(parsed, input);
    } catch (schemaErr) {
      return safeError('MODEL_RESPONSE_INVALID', `Gemini response failed validation: ${schemaErr.message}`);
    }

    return { ok: true, refinedDraft };
  } catch (err) {
    if (err?.name === 'AbortError') return safeError('TIMEOUT', 'Gemini request timed out.');
    if (err instanceof TypeError) return safeError('NETWORK_ERROR', 'Gemini request failed because the network request could not be completed.');
    return safeError('MODEL_RESPONSE_INVALID', 'Gemini response handling failed unexpectedly.');
  } finally {
    clearTimeout(timeout);
  }
});

// ── Embedded static file server ──────────────────────────────────────────────
// Serves index.html (and local static assets) from the project root over HTTP so
// that Google Drive OAuth, PDF.js workers, and Tesseract workers can all use an
// approved HTTP/HTTPS origin. Binds to 127.0.0.1 only. Not used when
// NBME_ELECTRON_URL is set.

const PROJECT_ROOT = path.resolve(__dirname, '..');

const MIME = {
  '.html':  'text/html; charset=utf-8',
  '.js':    'application/javascript; charset=utf-8',
  '.css':   'text/css; charset=utf-8',
  '.json':  'application/json; charset=utf-8',
  '.png':   'image/png',
  '.jpg':   'image/jpeg',
  '.jpeg':  'image/jpeg',
  '.webp':  'image/webp',
  '.svg':   'image/svg+xml',
  '.pdf':   'application/pdf',
  '.woff':  'font/woff',
  '.woff2': 'font/woff2',
  '.wasm':  'application/wasm',
  '.txt':   'text/plain; charset=utf-8'
};

const LOCAL_FIGURE_MIME = {
  '.png':  'image/png',
  '.jpg':  'image/jpeg',
  '.jpeg': 'image/jpeg',
  '.webp': 'image/webp'
};

function resolveLocalPath(rawUrl) {
  try {
    const clean = decodeURIComponent((rawUrl || '/').split('?')[0].split('#')[0]) || '/';
    const rel = clean.startsWith('/') ? clean : '/' + clean;
    const abs = path.resolve(PROJECT_ROOT, '.' + rel);
    // Reject any path that escapes the project root (path traversal guard).
    if (abs !== PROJECT_ROOT && !abs.startsWith(PROJECT_ROOT + path.sep)) return null;
    return abs;
  } catch (_) {
    return null;
  }
}

function serveIndexHtml(res, reason) {
  const indexPath = path.join(PROJECT_ROOT, 'index.html');
  console.log('[NBME ROUTE /] Serving index from:', indexPath, reason ? `(reason: ${reason})` : '');
  try {
    const indexHtml = fs.readFileSync(indexPath, 'utf8');
    console.log('[NBME INDEX MARKER PRESENT]', indexHtml.includes('APP_BUILD_MARKER'));
  } catch(e) {
    console.log('[NBME INDEX MARKER PRESENT] error reading file synchronously:', e.message);
  }
  fs.readFile(indexPath, (err, data) => {
    if (err) { res.writeHead(500); res.end(); return; }
    res.writeHead(200, { 'Content-Type': 'text/html; charset=utf-8', 'Cache-Control': 'no-cache' });
    res.end(data);
  });
}

function resolveExternalFigurePath(rawPath) {
  const raw = String(rawPath || '').trim();
  if (!raw) return null;
  try {
    if (/^file:/i.test(raw)) return decodeURIComponent(new URL(raw).pathname);
    return path.resolve(raw);
  } catch (_) {
    return null;
  }
}

function serveExternalFigure(req, res) {
  let parsed;
  try {
    parsed = new URL(req.url, 'http://127.0.0.1');
  } catch (_) {
    res.writeHead(400); res.end(); return;
  }
  const localPath = resolveExternalFigurePath(parsed.searchParams.get('path'));
  if (!localPath) { res.writeHead(400); res.end(); return; }
  const ext = path.extname(localPath).toLowerCase();
  const contentType = LOCAL_FIGURE_MIME[ext];
  if (!contentType) { res.writeHead(403); res.end(); return; }
  fs.stat(localPath, (err, stat) => {
    if (err || !stat.isFile()) { res.writeHead(404); res.end(); return; }
    fs.readFile(localPath, (readErr, data) => {
      if (readErr) { res.writeHead(500); res.end(); return; }
      res.writeHead(200, { 'Content-Type': contentType, 'Cache-Control': 'no-cache' });
      res.end(data);
    });
  });
}

function createRequestHandler() {
  return function (req, res) {
    console.log('[NBME REQUEST]', req.method, req.url);
    if ((req.url || '').startsWith('/__nbme_local_figure__')) {
      return serveExternalFigure(req, res);
    }
    const localPath = resolveLocalPath(req.url);
    if (!localPath) return serveIndexHtml(res, 'bad URL'); // bad URL → SPA fallback

    fs.stat(localPath, (err, stat) => {
      if (err || !stat.isFile()) return serveIndexHtml(res, `not found: ${localPath}`); // not found → SPA fallback

      const ext = path.extname(localPath).toLowerCase();
      const contentType = MIME[ext];
      if (!contentType) { res.writeHead(403); res.end(); return; } // unknown type → deny

      fs.readFile(localPath, (readErr, data) => {
        if (readErr) { res.writeHead(500); res.end(); return; }
        res.writeHead(200, { 'Content-Type': contentType, 'Cache-Control': 'no-cache' });
        res.end(data);
      });
    });
  };
}

function tryListenOnPort(handler, port) {
  return new Promise((resolve) => {
    const server = http.createServer(handler);
    server.once('error', () => { server.close(); resolve(null); });
    server.once('listening', () => resolve(server));
    server.listen(port, '127.0.0.1');
  });
}

async function startEmbeddedServer() {
  const handler = createRequestHandler();
  for (const port of [8888, 8080]) {
    const server = await tryListenOnPort(handler, port);
    if (server) return server;
  }
  // Fall back to an OS-assigned port (port 0).
  const server = await tryListenOnPort(handler, 0);
  if (!server) throw new Error('[NBME] Embedded HTTP server failed to bind on any port.');
  return server;
}

let _embeddedServer = null;

// ─────────────────────────────────────────────────────────────────────────────

// ── Application menu ─────────────────────────────────────────────────────────
// Provides Cmd+R / Cmd+Shift+R reload shortcuts and a standard macOS menu bar.
// Without this, Electron uses its default menu which has no reload shortcut and
// may not wire Cmd+Q correctly when the window is in a custom state.
function buildAppMenu(win) {
  return Menu.buildFromTemplate([
    {
      label: app.name,
      submenu: [
        { role: 'about' },
        { type: 'separator' },
        { role: 'services' },
        { type: 'separator' },
        { role: 'hide' },
        { role: 'hideOthers' },
        { role: 'unhide' },
        { type: 'separator' },
        { role: 'quit' }
      ]
    },
    {
      label: 'Edit',
      submenu: [
        { role: 'undo' },
        { role: 'redo' },
        { type: 'separator' },
        { role: 'cut' },
        { role: 'copy' },
        { role: 'paste' },
        { role: 'selectAll' }
      ]
    },
    {
      label: 'View',
      submenu: [
        {
          label: 'Reload',
          accelerator: 'CmdOrCtrl+R',
          click: () => {
            const w = BrowserWindow.getFocusedWindow();
            if (w) w.webContents.reload();
          }
        },
        {
          label: 'Hard Reload',
          accelerator: 'CmdOrCtrl+Shift+R',
          click: () => {
            const w = BrowserWindow.getFocusedWindow();
            if (w) w.webContents.reloadIgnoringCache();
          }
        },
        { type: 'separator' },
        { role: 'toggleDevTools' },
        { type: 'separator' },
        { role: 'togglefullscreen' }
      ]
    },
    {
      label: 'Window',
      submenu: [
        { role: 'minimize' },
        { role: 'zoom' },
        { type: 'separator' },
        { role: 'close' }
      ]
    }
  ]);
}

// Main process boundary:
// Owns Electron window lifecycle and loading the existing HTTP-served app only.
// App logic, parser/OCR/render behavior, Drive, and storage remain in index.html.
// AI status is owned here so future Gemini calls can remain outside the renderer.
function createWindow() {
  const win = new BrowserWindow({
    width: 1440,
    height: 960,
    minWidth: 1100,
    minHeight: 700,
    show: false,
    webPreferences: {
      preload: path.join(__dirname, 'preload.js'),
      contextIsolation: true,
      nodeIntegration: false,
      sandbox: true,
      webSecurity: true,
      allowRunningInsecureContent: false
    }
  });

  win.once('ready-to-show', () => {
    win.show();
  });

  // The renderer's Drive IIFE registers a beforeunload handler that calls
  // e.preventDefault() while a Drive sync is pending.  In Electron this fires
  // will-prevent-unload on the webContents; without overriding it the window
  // close silently does nothing — the red X appears to do nothing at all.
  // We override it here and handle state flushing ourselves in the close handler.
  win.webContents.on('will-prevent-unload', (e) => {
    e.preventDefault(); // allow the unload — flush already handled below
  });

  // Flush renderer state then close.  Two-pass pattern: first pass defers the
  // close to run the flush; second pass (isClosing=true) lets it proceed.
  let isClosing = false;
  win.on('close', async (e) => {
    if (isClosing) return;
    e.preventDefault();
    isClosing = true;

    try {
      // Give the renderer up to 6 s to flush Drive sync and localStorage.
      // v6.1: route through __driveFlushForQuit() — the newest-wins close path.
      // It PUSHES local-newer edits, but NEVER overwrites a fresher Drive copy
      // (the old blind saveGoogleDriveNow() could clobber newer remote data —
      // the exact data-loss bug we're fixing). On divergence it writes a dated
      // conflict copy so nothing is lost. Falls back to the old push only if the
      // new entry point is somehow absent. Bounded by the race timeout so a slow
      // or offline network can never hang the quit.
      const flushResult = await Promise.race([
        win.webContents.executeJavaScript(`(async () => {
          try {
            if (typeof window.__driveFlushForQuit === 'function') {
              return await window.__driveFlushForQuit();
            }
            if (typeof window.saveGoogleDriveNow === 'function') {
              await window.saveGoogleDriveNow();
            }
            if (typeof DB !== 'undefined' && typeof DB.save === 'function') {
              DB.save({ sync: true });
            }
            return 'legacy-flush';
          } catch (_) {
            return 'flush-error';
          }
        })()`),
        new Promise(resolve => setTimeout(() => resolve('timeout'), 6000))
      ]);
      console.log('[NBME] Drive flush on close:', flushResult);
    } catch (err) {
      console.error('[NBME] State flush failed on close:', err.message);
    }

    win.close(); // second pass — isClosing is true, close proceeds
  });

  win.loadURL(resolvedDevUrl);
}

app.whenReady().then(async () => {
  reconcileQueueAndHistoryOnStartup();
  if (process.env.NBME_ELECTRON_URL) {
    resolvedDevUrl = process.env.NBME_ELECTRON_URL;
    console.log('[NBME] Using external URL override:', resolvedDevUrl);
  } else {
    _embeddedServer = await startEmbeddedServer();
    const { port } = _embeddedServer.address();
    resolvedDevUrl = `http://localhost:${port}`;
    console.log('[NBME] Embedded server listening at:', resolvedDevUrl);
  }

  createWindow();
  scheduleBatchQueue();

  app.on('activate', () => {
    if (BrowserWindow.getAllWindows().length === 0) createWindow();
  });
});

// Always quit when all windows are closed.  This is a single-window app;
// the standard macOS "keep app alive in dock" behavior is not appropriate here.
// Without this, the embedded HTTP server keeps the process alive after the
// window is closed, requiring a force-quit.
app.on('window-all-closed', () => {
  app.quit();
});

app.on('before-quit', event => {
  try {
    if (batchShutdownInProgress) return;
    batchShutdownInProgress = true;
    cleanupTrackedBatchProcessesForJobs(readBatchQueueRaw().jobs, 'before-quit queue registry');
    cleanupTrackedBatchProcessesForJobs(readBatchJobHistoryRaw().jobs, 'before-quit history registry');
    interruptActiveBatchJobsForShutdown();
    if (_embeddedServer) {
      _embeddedServer.close();
      _embeddedServer = null;
    }
    if (activeBatchJobs.size) {
      event.preventDefault();
      setTimeout(() => {
        try {
          activeBatchJobs.forEach((active, jobId) => {
            if (active.proc && active.proc.exitCode === null) {
              safeKillProcessGroup(active.proc, 'SIGKILL', `shutdown fallback ${jobId}`);
            } else {
              console.warn(`[NBME] Batch process kill skipped (shutdown fallback ${jobId}): process missing or already closed.`);
            }
          });
          activeBatchJobs.clear();
        } catch (err) {
          console.warn(`[NBME] Batch shutdown cleanup failed safely: ${err.message || String(err)}`);
        }
        app.exit(0);
      }, 5000);
    }
  } catch (err) {
    console.warn(`[NBME] before-quit cleanup failed safely: ${err.message || String(err)}`);
  }
});

// Set menu after app is ready (called from whenReady chain above via createWindow,
// but Menu.setApplicationMenu can be called any time — set it once on first window).
app.on('browser-window-created', (_e, win) => {
  Menu.setApplicationMenu(buildAppMenu(win));
});
