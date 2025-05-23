import asyncio
from datetime import datetime
from typing import Any

import strawberry
from openinference.semconv.trace import (
    MessageAttributes,
    MessageContentAttributes,
    SpanAttributes,
    ToolAttributes,
    ToolCallAttributes,
)
from sqlalchemy import and_, delete, distinct, func, insert, select, update
from sqlalchemy.orm import contains_eager
from strawberry import UNSET
from strawberry.relay.types import GlobalID
from strawberry.types import Info

from phoenix.db import models
from phoenix.db.helpers import get_eval_trace_ids_for_datasets, get_project_names_for_datasets
from phoenix.server.api.auth import IsLocked, IsNotReadOnly
from phoenix.server.api.context import Context
from phoenix.server.api.exceptions import BadRequest, NotFound
from phoenix.server.api.helpers.dataset_helpers import (
    get_dataset_example_input,
    get_dataset_example_output,
)
from phoenix.server.api.input_types.AddExamplesToDatasetInput import AddExamplesToDatasetInput
from phoenix.server.api.input_types.AddSpansToDatasetInput import AddSpansToDatasetInput
from phoenix.server.api.input_types.CreateDatasetInput import CreateDatasetInput
from phoenix.server.api.input_types.DeleteDatasetExamplesInput import DeleteDatasetExamplesInput
from phoenix.server.api.input_types.DeleteDatasetInput import DeleteDatasetInput
from phoenix.server.api.input_types.PatchDatasetExamplesInput import (
    DatasetExamplePatch,
    PatchDatasetExamplesInput,
)
from phoenix.server.api.input_types.PatchDatasetInput import PatchDatasetInput
from phoenix.server.api.types.Dataset import Dataset, to_gql_dataset
from phoenix.server.api.types.DatasetExample import DatasetExample
from phoenix.server.api.types.node import from_global_id_with_expected_type
from phoenix.server.api.types.Span import Span
from phoenix.server.api.utils import delete_projects, delete_traces
from phoenix.server.dml_event import DatasetDeleteEvent, DatasetInsertEvent


@strawberry.type
class DatasetMutationPayload:
    dataset: Dataset


@strawberry.type
class DatasetMutationMixin:
    @strawberry.mutation(permission_classes=[IsNotReadOnly, IsLocked])  # type: ignore
    async def create_dataset(
        self,
        info: Info[Context, None],
        input: CreateDatasetInput,
    ) -> DatasetMutationPayload:
        name = input.name
        description = input.description if input.description is not UNSET else None
        metadata = input.metadata
        async with info.context.db() as session:
            dataset = await session.scalar(
                insert(models.Dataset)
                .values(
                    name=name,
                    description=description,
                    metadata_=metadata,
                )
                .returning(models.Dataset)
            )
            assert dataset is not None
        info.context.event_queue.put(DatasetInsertEvent((dataset.id,)))
        return DatasetMutationPayload(dataset=to_gql_dataset(dataset))

    @strawberry.mutation(permission_classes=[IsNotReadOnly, IsLocked])  # type: ignore
    async def patch_dataset(
        self,
        info: Info[Context, None],
        input: PatchDatasetInput,
    ) -> DatasetMutationPayload:
        dataset_id = from_global_id_with_expected_type(
            global_id=input.dataset_id, expected_type_name=Dataset.__name__
        )
        patch = {
            column.key: patch_value
            for column, patch_value, column_is_nullable in (
                (models.Dataset.name, input.name, False),
                (models.Dataset.description, input.description, True),
                (models.Dataset.metadata_, input.metadata, False),
            )
            if patch_value is not UNSET and (patch_value is not None or column_is_nullable)
        }
        async with info.context.db() as session:
            dataset = await session.scalar(
                update(models.Dataset)
                .where(models.Dataset.id == dataset_id)
                .returning(models.Dataset)
                .values(**patch)
            )
            assert dataset is not None
        info.context.event_queue.put(DatasetInsertEvent((dataset.id,)))
        return DatasetMutationPayload(dataset=to_gql_dataset(dataset))

    @strawberry.mutation(permission_classes=[IsNotReadOnly, IsLocked])  # type: ignore
    async def add_spans_to_dataset(
        self,
        info: Info[Context, None],
        input: AddSpansToDatasetInput,
    ) -> DatasetMutationPayload:
        dataset_id = input.dataset_id
        span_ids = input.span_ids
        dataset_version_description = (
            input.dataset_version_description
            if isinstance(input.dataset_version_description, str)
            else None
        )
        dataset_version_metadata = input.dataset_version_metadata
        dataset_rowid = from_global_id_with_expected_type(
            global_id=dataset_id, expected_type_name=Dataset.__name__
        )
        span_rowids = {
            from_global_id_with_expected_type(global_id=span_id, expected_type_name=Span.__name__)
            for span_id in set(span_ids)
        }
        async with info.context.db() as session:
            if (
                dataset := await session.scalar(
                    select(models.Dataset).where(models.Dataset.id == dataset_rowid)
                )
            ) is None:
                raise ValueError(
                    f"Unknown dataset: {dataset_id}"
                )  # todo: implement error types https://github.com/Arize-ai/phoenix/issues/3221
            dataset_version = models.DatasetVersion(
                dataset_id=dataset_rowid,
                description=dataset_version_description,
                metadata_=dataset_version_metadata or {},
            )
            session.add(dataset_version)
            await session.flush()
            spans = (
                (
                    await session.scalars(
                        select(models.Span)
                        .outerjoin(
                            models.SpanAnnotation,
                            models.Span.id == models.SpanAnnotation.span_rowid,
                        )
                        .outerjoin(models.User, models.SpanAnnotation.user_id == models.User.id)
                        .order_by(
                            models.Span.id,
                            models.SpanAnnotation.name,
                            models.User.username,
                        )
                        .where(models.Span.id.in_(span_rowids))
                        .options(
                            contains_eager(models.Span.span_annotations).contains_eager(
                                models.SpanAnnotation.user
                            )
                        )
                    )
                )
                .unique()
                .all()
            )
            if span_rowids - {span.id for span in spans}:
                raise NotFound("Some spans could not be found")

            DatasetExample = models.DatasetExample
            dataset_example_rowids = (
                await session.scalars(
                    insert(DatasetExample).returning(DatasetExample.id),
                    [
                        {
                            DatasetExample.dataset_id.key: dataset_rowid,
                            DatasetExample.span_rowid.key: span.id,
                        }
                        for span in spans
                    ],
                )
            ).all()
            assert len(dataset_example_rowids) == len(spans)
            assert all(map(lambda id: isinstance(id, int), dataset_example_rowids))
            DatasetExampleRevision = models.DatasetExampleRevision

            all_span_attributes = {
                **SpanAttributes.__dict__,
                **MessageAttributes.__dict__,
                **MessageContentAttributes.__dict__,
                **ToolCallAttributes.__dict__,
                **ToolAttributes.__dict__,
            }
            nonprivate_span_attributes = {
                k: v for k, v in all_span_attributes.items() if not k.startswith("_")
            }

            await session.execute(
                insert(DatasetExampleRevision),
                [
                    {
                        DatasetExampleRevision.dataset_example_id.key: dataset_example_rowid,
                        DatasetExampleRevision.dataset_version_id.key: dataset_version.id,
                        DatasetExampleRevision.input.key: get_dataset_example_input(span),
                        DatasetExampleRevision.output.key: get_dataset_example_output(span),
                        DatasetExampleRevision.metadata_.key: {
                            **(span.attributes.get(SpanAttributes.METADATA) or dict()),
                            **{
                                k: v
                                for k, v in span.attributes.items()
                                if k in nonprivate_span_attributes
                            },
                            "span_kind": span.span_kind,
                            "annotations": _gather_span_annotations_by_name(span.span_annotations),
                        },
                        DatasetExampleRevision.revision_kind.key: "CREATE",
                    }
                    for dataset_example_rowid, span in zip(dataset_example_rowids, spans)
                ],
            )
        info.context.event_queue.put(DatasetInsertEvent((dataset.id,)))
        return DatasetMutationPayload(dataset=to_gql_dataset(dataset))

    @strawberry.mutation(permission_classes=[IsNotReadOnly, IsLocked])  # type: ignore
    async def add_examples_to_dataset(
        self, info: Info[Context, None], input: AddExamplesToDatasetInput
    ) -> DatasetMutationPayload:
        dataset_id = input.dataset_id
        # Extract the span rowids from the input examples if they exist
        span_ids = [example.span_id for example in input.examples if example.span_id]
        span_rowids = {
            from_global_id_with_expected_type(global_id=span_id, expected_type_name=Span.__name__)
            for span_id in set(span_ids)
        }
        dataset_version_description = (
            input.dataset_version_description if input.dataset_version_description else None
        )
        dataset_version_metadata = input.dataset_version_metadata
        dataset_rowid = from_global_id_with_expected_type(
            global_id=dataset_id, expected_type_name=Dataset.__name__
        )
        async with info.context.db() as session:
            if (
                dataset := await session.scalar(
                    select(models.Dataset).where(models.Dataset.id == dataset_rowid)
                )
            ) is None:
                raise ValueError(
                    f"Unknown dataset: {dataset_id}"
                )  # todo: implement error types https://github.com/Arize-ai/phoenix/issues/3221
            dataset_version_rowid = await session.scalar(
                insert(models.DatasetVersion)
                .values(
                    dataset_id=dataset_rowid,
                    description=dataset_version_description,
                    metadata_=dataset_version_metadata,
                )
                .returning(models.DatasetVersion.id)
            )

            # Fetch spans and span annotations
            spans = (
                await session.execute(
                    select(models.Span.id)
                    .select_from(models.Span)
                    .where(models.Span.id.in_(span_rowids))
                )
            ).all()

            span_annotations = (
                await session.execute(
                    select(
                        models.SpanAnnotation.span_rowid,
                        models.SpanAnnotation.name,
                        models.SpanAnnotation.label,
                        models.SpanAnnotation.score,
                        models.SpanAnnotation.explanation,
                        models.SpanAnnotation.metadata_,
                        models.SpanAnnotation.annotator_kind,
                    )
                    .select_from(models.SpanAnnotation)
                    .where(models.SpanAnnotation.span_rowid.in_(span_rowids))
                )
            ).all()

            span_annotations_by_span: dict[int, dict[Any, Any]] = {span.id: {} for span in spans}
            for annotation in span_annotations:
                span_id = annotation.span_rowid
                if span_id not in span_annotations_by_span:
                    span_annotations_by_span[span_id] = dict()
                span_annotations_by_span[span_id][annotation.name] = {
                    "label": annotation.label,
                    "score": annotation.score,
                    "explanation": annotation.explanation,
                    "metadata": annotation.metadata_,
                    "annotator_kind": annotation.annotator_kind,
                }

            DatasetExample = models.DatasetExample
            dataset_example_rowids = (
                await session.scalars(
                    insert(DatasetExample).returning(DatasetExample.id),
                    [
                        {
                            DatasetExample.dataset_id.key: dataset_rowid,
                            DatasetExample.span_rowid.key: from_global_id_with_expected_type(
                                global_id=example.span_id,
                                expected_type_name=Span.__name__,
                            )
                            if example.span_id
                            else None,
                        }
                        for example in input.examples
                    ],
                )
            ).all()
            assert len(dataset_example_rowids) == len(input.examples)
            assert all(map(lambda id: isinstance(id, int), dataset_example_rowids))
            DatasetExampleRevision = models.DatasetExampleRevision

            dataset_example_revisions = []
            for dataset_example_rowid, example in zip(dataset_example_rowids, input.examples):
                span_annotation = {}
                if example.span_id:
                    span_id = from_global_id_with_expected_type(
                        global_id=example.span_id,
                        expected_type_name=Span.__name__,
                    )
                    span_annotation = span_annotations_by_span.get(span_id, {})
                dataset_example_revisions.append(
                    {
                        DatasetExampleRevision.dataset_example_id.key: dataset_example_rowid,
                        DatasetExampleRevision.dataset_version_id.key: dataset_version_rowid,
                        DatasetExampleRevision.input.key: example.input,
                        DatasetExampleRevision.output.key: example.output,
                        DatasetExampleRevision.metadata_.key: {
                            **(example.metadata or {}),
                            "annotations": span_annotation,
                        },
                        DatasetExampleRevision.revision_kind.key: "CREATE",
                    }
                )
            await session.execute(
                insert(DatasetExampleRevision),
                dataset_example_revisions,
            )
        info.context.event_queue.put(DatasetInsertEvent((dataset.id,)))
        return DatasetMutationPayload(dataset=to_gql_dataset(dataset))

    @strawberry.mutation(permission_classes=[IsNotReadOnly])  # type: ignore
    async def delete_dataset(
        self,
        info: Info[Context, None],
        input: DeleteDatasetInput,
    ) -> DatasetMutationPayload:
        try:
            dataset_id = from_global_id_with_expected_type(
                global_id=input.dataset_id,
                expected_type_name=Dataset.__name__,
            )
        except ValueError:
            raise NotFound(f"Unknown dataset: {input.dataset_id}")
        project_names_stmt = get_project_names_for_datasets(dataset_id)
        eval_trace_ids_stmt = get_eval_trace_ids_for_datasets(dataset_id)
        stmt = (
            delete(models.Dataset).where(models.Dataset.id == dataset_id).returning(models.Dataset)
        )
        async with info.context.db() as session:
            project_names = await session.scalars(project_names_stmt)
            eval_trace_ids = await session.scalars(eval_trace_ids_stmt)
            if not (dataset := await session.scalar(stmt)):
                raise NotFound(f"Unknown dataset: {input.dataset_id}")
        await asyncio.gather(
            delete_projects(info.context.db, *project_names),
            delete_traces(info.context.db, *eval_trace_ids),
            return_exceptions=True,
        )
        info.context.event_queue.put(DatasetDeleteEvent((dataset.id,)))
        return DatasetMutationPayload(dataset=to_gql_dataset(dataset))

    @strawberry.mutation(permission_classes=[IsNotReadOnly, IsLocked])  # type: ignore
    async def patch_dataset_examples(
        self,
        info: Info[Context, None],
        input: PatchDatasetExamplesInput,
    ) -> DatasetMutationPayload:
        if not (patches := input.patches):
            raise BadRequest("Must provide examples to patch.")
        by_numeric_id = [
            (
                from_global_id_with_expected_type(patch.example_id, DatasetExample.__name__),
                index,
                patch,
            )
            for index, patch in enumerate(patches)
        ]
        example_ids, _, patches = map(list, zip(*sorted(by_numeric_id)))
        if len(set(example_ids)) < len(example_ids):
            raise BadRequest("Cannot patch the same example more than once per mutation.")
        if any(patch.is_empty() for patch in patches):
            raise BadRequest("Received one or more empty patches that contain no fields to update.")
        version_description = input.version_description or None
        version_metadata = input.version_metadata
        async with info.context.db() as session:
            datasets = (
                await session.scalars(
                    select(models.Dataset)
                    .where(
                        models.Dataset.id.in_(
                            select(distinct(models.DatasetExample.dataset_id))
                            .where(models.DatasetExample.id.in_(example_ids))
                            .scalar_subquery()
                        )
                    )
                    .limit(2)
                )
            ).all()
            if not datasets:
                raise NotFound("No examples found.")
            if len(set(ds.id for ds in datasets)) > 1:
                raise BadRequest("Examples must come from the same dataset.")
            dataset = datasets[0]

            revision_ids = (
                select(func.max(models.DatasetExampleRevision.id))
                .where(models.DatasetExampleRevision.dataset_example_id.in_(example_ids))
                .group_by(models.DatasetExampleRevision.dataset_example_id)
                .scalar_subquery()
            )
            revisions = (
                await session.scalars(
                    select(models.DatasetExampleRevision)
                    .where(
                        and_(
                            models.DatasetExampleRevision.id.in_(revision_ids),
                            models.DatasetExampleRevision.revision_kind != "DELETE",
                        )
                    )
                    .order_by(
                        models.DatasetExampleRevision.dataset_example_id
                    )  # ensure the order of the revisions matches the order of the input patches
                )
            ).all()
            if (num_missing_examples := len(example_ids) - len(revisions)) > 0:
                raise NotFound(f"{num_missing_examples} example(s) could not be found.")

            version_id = await session.scalar(
                insert(models.DatasetVersion)
                .returning(models.DatasetVersion.id)
                .values(
                    dataset_id=dataset.id,
                    description=version_description,
                    metadata_=version_metadata,
                )
            )
            assert version_id is not None

            await session.execute(
                insert(models.DatasetExampleRevision),
                [
                    _to_orm_revision(
                        existing_revision=revision,
                        patch=patch,
                        example_id=example_id,
                        version_id=version_id,
                    )
                    for revision, patch, example_id in zip(revisions, patches, example_ids)
                ],
            )
        info.context.event_queue.put(DatasetInsertEvent((dataset.id,)))
        return DatasetMutationPayload(dataset=to_gql_dataset(dataset))

    @strawberry.mutation(permission_classes=[IsNotReadOnly, IsLocked])  # type: ignore
    async def delete_dataset_examples(
        self, info: Info[Context, None], input: DeleteDatasetExamplesInput
    ) -> DatasetMutationPayload:
        timestamp = datetime.now()
        example_db_ids = [
            from_global_id_with_expected_type(global_id, models.DatasetExample.__name__)
            for global_id in input.example_ids
        ]
        # Guard against empty input
        if not example_db_ids:
            raise ValueError("Must provide examples to delete")
        dataset_version_description = (
            input.dataset_version_description
            if isinstance(input.dataset_version_description, str)
            else None
        )
        dataset_version_metadata = input.dataset_version_metadata
        async with info.context.db() as session:
            # Check if the examples are from a single dataset
            datasets = (
                await session.scalars(
                    select(models.Dataset)
                    .join(
                        models.DatasetExample, models.Dataset.id == models.DatasetExample.dataset_id
                    )
                    .where(models.DatasetExample.id.in_(example_db_ids))
                    .distinct()
                    .limit(2)  # limit to 2 to check if there are more than 1 dataset
                )
            ).all()
            if len(datasets) > 1:
                raise ValueError("Examples must be from the same dataset")
            elif not datasets:
                raise ValueError("Examples not found")

            dataset = datasets[0]

            dataset_version_rowid = await session.scalar(
                insert(models.DatasetVersion)
                .values(
                    dataset_id=dataset.id,
                    description=dataset_version_description,
                    metadata_=dataset_version_metadata,
                    created_at=timestamp,
                )
                .returning(models.DatasetVersion.id)
            )

            # If the examples already have a delete revision, skip the deletion
            existing_delete_revisions = (
                await session.scalars(
                    select(models.DatasetExampleRevision).where(
                        models.DatasetExampleRevision.dataset_example_id.in_(example_db_ids),
                        models.DatasetExampleRevision.revision_kind == "DELETE",
                    )
                )
            ).all()

            if existing_delete_revisions:
                raise ValueError(
                    "Provided examples contain already deleted examples. Delete aborted."
                )

            DatasetExampleRevision = models.DatasetExampleRevision
            await session.execute(
                insert(DatasetExampleRevision),
                [
                    {
                        DatasetExampleRevision.dataset_example_id.key: dataset_example_rowid,
                        DatasetExampleRevision.dataset_version_id.key: dataset_version_rowid,
                        DatasetExampleRevision.input.key: {},
                        DatasetExampleRevision.output.key: {},
                        DatasetExampleRevision.metadata_.key: {},
                        DatasetExampleRevision.revision_kind.key: "DELETE",
                        DatasetExampleRevision.created_at.key: timestamp,
                    }
                    for dataset_example_rowid in example_db_ids
                ],
            )
        info.context.event_queue.put(DatasetInsertEvent((dataset.id,)))
        return DatasetMutationPayload(dataset=to_gql_dataset(dataset))


def _span_attribute(semconv: str) -> Any:
    """
    Extracts an attribute from the ORM span attributes column and labels the
    result.

    E.g., "input.value" -> Span.attributes["input"]["value"].label("input_value")
    """
    attribute_value: Any = models.Span.attributes
    for key in semconv.split("."):
        attribute_value = attribute_value[key]
    return attribute_value.label(semconv.replace(".", "_"))


def _to_orm_revision(
    *,
    existing_revision: models.DatasetExampleRevision,
    patch: DatasetExamplePatch,
    example_id: int,
    version_id: int,
) -> dict[str, Any]:
    """
    Creates a new revision from an existing revision and a patch. The output is a
    dictionary suitable for insertion into the database using the sqlalchemy
    bulk insertion API.
    """

    db_rev = models.DatasetExampleRevision
    input = patch.input if isinstance(patch.input, dict) else existing_revision.input
    output = patch.output if isinstance(patch.output, dict) else existing_revision.output
    metadata = patch.metadata if isinstance(patch.metadata, dict) else existing_revision.metadata_
    return {
        str(db_column.key): patch_value
        for db_column, patch_value in (
            (db_rev.dataset_example_id, example_id),
            (db_rev.dataset_version_id, version_id),
            (db_rev.input, input),
            (db_rev.output, output),
            (db_rev.metadata_, metadata),
            (db_rev.revision_kind, "PATCH"),
        )
    }


def _gather_span_annotations_by_name(
    span_annotations: list[models.SpanAnnotation],
) -> dict[str, list[dict[str, Any]]]:
    span_annotations_by_name: dict[str, list[dict[str, Any]]] = {}
    for span_annotation in span_annotations:
        if span_annotation.name not in span_annotations_by_name:
            span_annotations_by_name[span_annotation.name] = []
        span_annotations_by_name[span_annotation.name].append(
            _to_span_annotation_dict(span_annotation)
        )
    return span_annotations_by_name


def _to_span_annotation_dict(span_annotation: models.SpanAnnotation) -> dict[str, Any]:
    return {
        "label": span_annotation.label,
        "score": span_annotation.score,
        "explanation": span_annotation.explanation,
        "metadata": span_annotation.metadata_,
        "annotator_kind": span_annotation.annotator_kind,
        "user_id": str(GlobalID(models.User.__name__, str(user_id)))
        if (user_id := span_annotation.user_id) is not None
        else None,
        "username": user.username if (user := span_annotation.user) is not None else None,
        "email": user.email if user is not None else None,
    }


INPUT_MIME_TYPE = SpanAttributes.INPUT_MIME_TYPE
INPUT_VALUE = SpanAttributes.INPUT_VALUE
OUTPUT_MIME_TYPE = SpanAttributes.OUTPUT_MIME_TYPE
OUTPUT_VALUE = SpanAttributes.OUTPUT_VALUE
LLM_PROMPT_TEMPLATE_VARIABLES = SpanAttributes.LLM_PROMPT_TEMPLATE_VARIABLES
LLM_INPUT_MESSAGES = SpanAttributes.LLM_INPUT_MESSAGES
LLM_OUTPUT_MESSAGES = SpanAttributes.LLM_OUTPUT_MESSAGES
RETRIEVAL_DOCUMENTS = SpanAttributes.RETRIEVAL_DOCUMENTS
