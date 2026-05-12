import "server-only";

import type {
  ApiDataset,
  ApiExperiment,
  ApiJob,
  ApiList,
  ApiWorker,
} from "@/lib/api-types";
import {
  projectDataset,
  projectExperiment,
  projectJob,
  projectJobDetail,
  projectWorker,
} from "@/lib/api-projections";
import { demoDatasets, demoExperiments, demoJobs, demoWorkers } from "@/lib/demo-data";
import type {
  DatasetSummary,
  ExperimentSummary,
  JobDetail,
  JobStatus,
  JobSummary,
  ListLimitation,
  ListResult,
  UserRole,
  WorkerSummary,
} from "@/lib/types";
import { authenticatedManagerRequest, dashboardDemoMode } from "./auth";
import { ManagerApiError } from "./manager-api";

export interface OverviewData {
  jobs: ListResult<JobSummary>;
  workers: ListResult<WorkerSummary> | null;
}

export interface JobListFilters {
  status: JobStatus | null;
  experimentId: string | null;
}

export interface JobListData {
  jobs: ListResult<JobSummary>;
  experiments: Array<{ id: string; name: string }>;
  experimentLimitation?: ListLimitation;
}

export interface ExperimentWorkspaceData {
  id: string;
  rowVersion: number;
  name: string;
  description: string | null;
  dataset: {
    id: string;
    name: string;
    status: string;
    isUsable: boolean;
    fileCount: number | null;
    durationMinutes: number | null;
  };
  jobs: Array<{ id: string; name: string; status: JobStatus; createdAt: string | null }>;
  jobLimitation?: ListLimitation;
  demo: boolean;
}

export const DASHBOARD_PAGE_SIZE = 200;
export const DASHBOARD_COLLECTION_LIMIT = 10_000;

export class DashboardPaginationError extends Error {
  constructor(message: string) {
    super(message);
    this.name = "DashboardPaginationError";
  }
}

export async function loadOverviewData(role: UserRole): Promise<OverviewData> {
  if (dashboardDemoMode()) {
    return {
      jobs: { items: demoJobs, total: demoJobs.length },
      workers:
        role === "admin" ? { items: demoWorkers, total: demoWorkers.length } : null,
    };
  }
  const jobsPromise = loadApiJobs();
  const workersPromise = role === "admin" ? loadApiWorkers() : Promise.resolve(null);
  const [jobs, workers] = await Promise.all([jobsPromise, workersPromise]);
  return { jobs, workers };
}

export async function loadWorkersData(): Promise<ListResult<WorkerSummary>> {
  if (dashboardDemoMode()) return { items: demoWorkers, total: demoWorkers.length };
  return loadApiWorkers();
}

export async function loadDatasetsData(): Promise<ListResult<DatasetSummary>> {
  if (dashboardDemoMode()) return { items: demoDatasets, total: demoDatasets.length };
  const response = await loadCompleteApiList<ApiDataset>("datasets", "/api/v1/datasets");
  if (response.limitation) {
    return { items: [], total: response.total, limitation: response.limitation };
  }
  return { items: response.items.map(projectDataset), total: response.total };
}

export async function loadDatasetDetail(datasetId: string): Promise<DatasetSummary | null> {
  if (dashboardDemoMode()) {
    return demoDatasets.find((dataset) => dataset.id === datasetId) ?? null;
  }
  try {
    const response = await authenticatedManagerRequest<ApiDataset>(
      `/api/v1/datasets/${encodeURIComponent(datasetId)}`,
    );
    return projectDataset(response);
  } catch (error) {
    if (error instanceof ManagerApiError && error.status === 404) return null;
    throw error;
  }
}

export async function loadExperimentsData(): Promise<ListResult<ExperimentSummary>> {
  if (dashboardDemoMode()) {
    return { items: demoExperiments, total: demoExperiments.length };
  }
  const [experiments, datasets, jobs] = await Promise.all([
    loadCompleteApiList<ApiExperiment>("experiments", "/api/v1/experiments"),
    loadCompleteApiList<ApiDataset>("datasets", "/api/v1/datasets"),
    loadCompleteApiList<ApiJob>("jobs", "/api/v1/jobs"),
  ]);
  const limitation = experiments.limitation ?? datasets.limitation ?? jobs.limitation;
  if (limitation) {
    return { items: [], total: experiments.total, limitation };
  }
  const datasetMap = new Map(datasets.items.map((dataset) => [dataset.id, dataset]));
  return {
    items: experiments.items.map((experiment) =>
      projectExperiment(experiment, datasetMap, jobs.items),
    ),
    total: experiments.total,
  };
}

export async function loadExperimentWorkspace(
  experimentId: string,
): Promise<ExperimentWorkspaceData | null> {
  if (dashboardDemoMode()) {
    const experiment = demoExperiments.find((candidate) => candidate.id === experimentId);
    if (!experiment) return null;
    const dataset = demoDatasets.find(
      (candidate) => candidate.name === experiment.datasetName,
    );
    if (!dataset) return null;
    return {
      id: experiment.id,
      rowVersion: 1,
      name: experiment.name,
      description: null,
      dataset: {
        id: dataset.id,
        name: dataset.name,
        status: dataset.status,
        isUsable: dataset.status === "ready" && dataset.isUsable,
        fileCount: dataset.fileCount,
        durationMinutes: dataset.durationMinutes,
      },
      jobs: demoJobs
        .filter((job) => job.experiment === experiment.name)
        .map((job) => ({ id: job.id, name: job.name, status: job.status, createdAt: null })),
      demo: true,
    };
  }

  let experiment: ApiExperiment;
  try {
    experiment = await authenticatedManagerRequest<ApiExperiment>(
      `/api/v1/experiments/${encodeURIComponent(experimentId)}`,
    );
  } catch (error) {
    if (error instanceof ManagerApiError && error.status === 404) return null;
    throw error;
  }
  if (
    !Number.isSafeInteger(experiment.row_version) ||
    experiment.row_version < 1 ||
    experiment.row_version > 2_147_483_647
  ) {
    throw new Error("Manager returned an invalid Experiment row version");
  }
  const [dataset, jobs] = await Promise.all([
    authenticatedManagerRequest<ApiDataset>(
      `/api/v1/datasets/${encodeURIComponent(experiment.dataset_id)}`,
    ),
    loadCompleteApiList<ApiJob>("jobs", "/api/v1/jobs", {
      experiment_id: experiment.id,
    }),
  ]);
  return {
    id: experiment.id,
    rowVersion: experiment.row_version,
    name: experiment.name,
    description: experiment.description,
    dataset: {
      id: dataset.id,
      name: dataset.name,
      status: dataset.status,
      isUsable: dataset.status === "ready" && dataset.is_usable,
      fileCount: dataset.file_count,
      durationMinutes: dataset.duration_sec === null ? null : dataset.duration_sec / 60,
    },
    jobs: jobs.limitation ? [] : jobs.items.map((job) => ({
      id: job.id,
      name: job.job_name,
      status: job.status,
      createdAt: job.created_at,
    })),
    ...(jobs.limitation ? { jobLimitation: jobs.limitation } : {}),
    demo: false,
  };
}

export async function loadJobListData(filters: JobListFilters): Promise<JobListData> {
  if (dashboardDemoMode()) {
    const selectedExperiment = filters.experimentId
      ? demoExperiments.find((experiment) => experiment.id === filters.experimentId)
      : null;
    const items = demoJobs.filter(
      (job) =>
        (!filters.status || job.status === filters.status) &&
        (!filters.experimentId || job.experiment === selectedExperiment?.name),
    );
    return {
      jobs: { items, total: items.length },
      experiments: demoExperiments.map(({ id, name }) => ({ id, name })),
    };
  }
  const jobFilters: Record<string, string> = {};
  if (filters.status) jobFilters.status = filters.status;
  if (filters.experimentId) jobFilters.experiment_id = filters.experimentId;
  const [jobs, experiments] = await Promise.all([
    loadCompleteApiList<ApiJob>("jobs", "/api/v1/jobs", jobFilters),
    loadCompleteApiList<ApiExperiment>("experiments", "/api/v1/experiments"),
  ]);
  const displayLimitation = jobs.limitation ?? experiments.limitation;
  const experimentMap = new Map(
    experiments.items.map((experiment) => [experiment.id, experiment]),
  );
  return {
    jobs: {
      items: displayLimitation ? [] : jobs.items.map((job) => projectJob(job, experimentMap)),
      total: jobs.total,
      ...(displayLimitation ? { limitation: displayLimitation } : {}),
    },
    experiments: experiments.limitation
      ? []
      : experiments.items.map(({ id, name }) => ({ id, name })),
    ...(experiments.limitation
      ? { experimentLimitation: experiments.limitation }
      : {}),
  };
}

export async function loadJobDetail(jobId: string): Promise<JobDetail | null> {
  if (dashboardDemoMode()) {
    const job = demoJobs.find((candidate) => candidate.id === jobId);
    if (!job) return null;
    return {
      summary: job,
      experimentId: null,
      datasetId: null,
      workerId: null,
      currentAttemptId: null,
      priority: null,
      attemptCount: null,
      cancelRequestedAt: null,
      errorCode: null,
      errorMessage: null,
      startedAt: null,
      completedAt: null,
      createdAt: null,
      updatedAt: null,
      config: null,
      demo: true,
    };
  }

  let job: ApiJob;
  try {
    job = await authenticatedManagerRequest<ApiJob>(
      `/api/v1/jobs/${encodeURIComponent(jobId)}`,
    );
  } catch (error) {
    if (error instanceof ManagerApiError && error.status === 404) return null;
    throw error;
  }
  const experiment = await authenticatedManagerRequest<ApiExperiment>(
    `/api/v1/experiments/${encodeURIComponent(job.experiment_id)}`,
  );
  return projectJobDetail(job, new Map([[experiment.id, experiment]]));
}

async function loadApiWorkers(): Promise<ListResult<WorkerSummary>> {
  const response = await loadCompleteApiList<ApiWorker>("workers", "/api/v1/workers");
  if (response.limitation) {
    return { items: [], total: response.total, limitation: response.limitation };
  }
  return { items: response.items.map(projectWorker), total: response.total };
}

async function loadApiJobs(): Promise<ListResult<JobSummary>> {
  const [jobs, experiments] = await Promise.all([
    loadCompleteApiList<ApiJob>("jobs", "/api/v1/jobs"),
    loadCompleteApiList<ApiExperiment>("experiments", "/api/v1/experiments"),
  ]);
  const limitation = jobs.limitation ?? experiments.limitation;
  if (limitation) return { items: [], total: jobs.total, limitation };
  const experimentMap = new Map(
    experiments.items.map((experiment) => [experiment.id, experiment]),
  );
  return {
    items: jobs.items.map((job) => projectJob(job, experimentMap)),
    total: jobs.total,
  };
}

async function loadCompleteApiList<T>(
  resource: ListLimitation["resource"],
  pathname: `/api/v1/${string}`,
  filters: Readonly<Record<string, string>> = {},
): Promise<ListResult<T>> {
  const items: T[] = [];
  const seenIds = new Set<string>();
  let offset = 0;
  let expectedTotal: number | null = null;
  while (true) {
    const query = new URLSearchParams({
      limit: String(DASHBOARD_PAGE_SIZE),
      offset: String(offset),
    });
    for (const [key, value] of Object.entries(filters)) query.set(key, value);
    const page = await authenticatedManagerRequest<ApiList<T>>(`${pathname}?${query}`);
    assertPageEnvelope(page, offset);
    if (expectedTotal === null) {
      expectedTotal = page.total;
      if (expectedTotal > DASHBOARD_COLLECTION_LIMIT) {
        return {
          items: [],
          total: expectedTotal,
          limitation: {
            reason: "item_limit_exceeded",
            maximum: DASHBOARD_COLLECTION_LIMIT,
            total: expectedTotal,
            resource,
          },
        };
      }
    } else if (page.total !== expectedTotal) {
      throw new DashboardPaginationError(
        `${resource} total changed during pagination; reload is required`,
      );
    }
    for (const item of page.items) {
      const id = paginationItemId(item);
      if (!id || seenIds.has(id)) {
        throw new DashboardPaginationError(
          `${resource} page contains an invalid or duplicate item identifier`,
        );
      }
      seenIds.add(id);
      items.push(item);
    }
    if (items.length > DASHBOARD_COLLECTION_LIMIT) {
      throw new DashboardPaginationError(`${resource} exceeded the bounded collection limit`);
    }
    if (items.length === expectedTotal) return { items, total: expectedTotal };
    if (page.items.length === 0) {
      throw new DashboardPaginationError(`${resource} pagination did not advance`);
    }
    offset += page.items.length;
    if (offset > expectedTotal) {
      throw new DashboardPaginationError(`${resource} page exceeded the declared total`);
    }
  }
}

function assertPageEnvelope<T>(value: ApiList<T>, expectedOffset: number): void {
  if (
    !isRecord(value) ||
    !Array.isArray(value.items) ||
    !isSafeInteger(value.total, 0, Number.MAX_SAFE_INTEGER) ||
    value.offset !== expectedOffset ||
    value.limit !== DASHBOARD_PAGE_SIZE ||
    value.items.length > DASHBOARD_PAGE_SIZE ||
    expectedOffset + value.items.length > value.total
  ) {
    throw new DashboardPaginationError("Manager returned an invalid pagination envelope");
  }
}

function paginationItemId(value: unknown): string | null {
  if (!isRecord(value) || typeof value.id !== "string") return null;
  return /^[A-Za-z0-9][A-Za-z0-9_.-]{0,127}$/.test(value.id) ? value.id : null;
}

function isRecord(value: unknown): value is Record<string, unknown> {
  return typeof value === "object" && value !== null && !Array.isArray(value);
}

function isSafeInteger(value: unknown, minimum: number, maximum: number): value is number {
  return typeof value === "number" && Number.isSafeInteger(value) && value >= minimum && value <= maximum;
}
