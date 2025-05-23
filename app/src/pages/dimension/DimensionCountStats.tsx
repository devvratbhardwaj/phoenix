import { graphql, useFragment } from "react-relay";

import { Text } from "@phoenix/components";
import { intFormatter } from "@phoenix/utils/numberFormatUtils";

import { DimensionCountStats_dimension$key } from "./__generated__/DimensionCountStats_dimension.graphql";

export function DimensionCountStats(props: {
  dimension: DimensionCountStats_dimension$key;
}) {
  const data = useFragment<DimensionCountStats_dimension$key>(
    graphql`
      fragment DimensionCountStats_dimension on Dimension
      @argumentDefinitions(timeRange: { type: "TimeRange!" }) {
        id
        count: dataQualityMetric(metric: count, timeRange: $timeRange)
      }
    `,
    props.dimension
  );

  const count = data.count ?? 0;

  return (
    <>
      <Text elementType="h3" size="XS" color="text-700">
        Total Count
      </Text>
      <Text size="L">{intFormatter(count)}</Text>
    </>
  );
}
