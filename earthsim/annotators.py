"""
Helper functions for building interactive plots that support persistent user annotations.
"""

from functools import partial

import param
import numpy as np
import pandas as pd
import cartopy.crs as ccrs
import geopandas as gpd
import holoviews as hv
import geoviews as gv
import holoviews.plotting.bokeh

from holoviews import DynamicMap, Path, Table, NdOverlay, Store, Options
from holoviews.core.util import disable_constant
from holoviews.plotting.links import DataLink
from holoviews.streams import Selection1D, Stream, PolyDraw, PolyEdit, PointDraw, CDSStream
from geoviews.data.geopandas import GeoPandasInterface
from geoviews import Polygons, Points, WMTS, TriMesh

from .custom_tools import CheckpointTool, RestoreTool, ClearTool
from .links import VertexTableLink, PointTableLink


def poly_to_geopandas(polys, columns):
    """
    Converts a GeoViews Paths or Polygons type to a geopandas dataframe.

    Parameters
    ----------

    polys : gv.Path or gv.Polygons
        GeoViews element
    columns: list(str)
        List of columns

    Returns
    -------
    gdf : Geopandas dataframe
    """
    rows = []
    for g in polys.geom():
        rows.append(dict({c: '' for c in columns}, geometry=g))
    return gpd.GeoDataFrame(rows, columns=columns+['geometry'])


def initialize_tools(plot, element):
    """
    Initializes the Checkpoint and Restore tools.
    """
    cds = plot.handles['source']
    checkpoint = plot.state.select(type=CheckpointTool)
    restore = plot.state.select(type=RestoreTool)
    clear = plot.state.select(type=ClearTool)
    if checkpoint:
        checkpoint[0].sources.append(cds)
    if restore:
        restore[0].sources.append(cds)
    if clear:
        clear[0].sources.append(cds)


class GeoAnnotator(param.Parameterized):
    """
    Provides support for drawing polygons and points on top of a map.
    """

    tile_url = param.String(default='http://c.tile.openstreetmap.org/{Z}/{X}/{Y}.png',
                            doc="URL for the tile source", precedence=-1)

    extent = param.NumericTuple(default=(np.nan,)*4, doc="""
         Initial extent if no data is provided.""", precedence=-1)

    path_type = param.ClassSelector(default=Polygons, class_=Path, is_instance=False, doc="""
         The element type to draw into.""")

    height = param.Integer(default=500, doc="Height of the plot",
                           precedence=-1)

    width = param.Integer(default=900, doc="Width of the plot",
                          precedence=-1)

    def __init__(self, polys=None, points=None, crs=None, **params):
        super(GeoAnnotator, self).__init__(**params)
        plot_opts = dict(height=self.height, width=self.width)
        self.tiles = WMTS(self.tile_url, extents=self.extent,
                          crs=ccrs.PlateCarree()).opts(plot=plot_opts)
        polys = [] if polys is None else polys
        points = [] if points is None else points
        crs = ccrs.GOOGLE_MERCATOR if crs is None else crs
        tools = [CheckpointTool(), RestoreTool(), ClearTool()]
        opts = dict(tools=tools, finalize_hooks=[initialize_tools], color_index=None)
        if not isinstance(polys, Path):
            polys = self.path_type(polys, crs=crs).options(**opts)
        self.polys = polys.options(**opts)
        self.poly_stream = PolyDraw(source=self.polys, data={}, show_vertices=True)
        self.vertex_stream = PolyEdit(source=self.polys)
        if isinstance(points, Points):
            self.points = points
        else:
            self.points = Points(points, self.polys.kdims, crs=crs).options(**opts)
        self.point_stream = PointDraw(source=self.points, data={})

    def pprint(self):
        params = dict(self.get_param_values())
        name = params.pop('name')
        string = '%s\n%s\n\n' % (name, '-'*len(name))
        for item in sorted(params.items()):
            string += '  %s: %s\n' % (item)
        print(string)

    def view(self):
        return self.tiles * self.polys * self.points


class PointWidgetAnnotator(GeoAnnotator):
    """
    Allows adding a group to the currently selected points using
    a dropdown menu and add button. The current annotations are
    reflected in a table.

    Works by using a shared datasource on the Points and Table.
    """

    add = param.Action(default=lambda self: self.add_group(), precedence=1,
                       doc="""Button triggering add action.""")

    group = param.ObjectSelector()

    column = param.String(default='Group', constant=True)

    table_height = param.Integer(default=150, doc="Height of the table",
                                 precedence=-1)

    def __init__(self, groups, **params):
        super(PointWidgetAnnotator, self).__init__(**params)
        group_param = self.params('group')
        group_param.objects = groups
        group_param.default = groups[0]
        self.point_sel_stream = Selection1D(source=self.points)
        self._group_data = {g: [] for g in groups}
        self.table_stream = Stream.define('TableUpdate')(transient=True)

    def add_group(self, **kwargs):
        new_index = self.point_sel_stream.index
        for idx in new_index:
            if idx not in self._group_data[self.group]:
                self._group_data[self.group].append(idx)
            for g, inds in self._group_data.items():
                if g != self.group:
                    self._group_data[g] = [idx for idx in inds if idx not in new_index]
        self.table_stream.trigger([self.table_stream])

    def group_table(self):
        plot = dict(width=self.width, height=self.table_height)
        data = [(group, str(inds)) for group, inds in self._group_data.items()]
        return Table(data, self.column, 'index').sort().opts(plot=plot)

    def annotated_points(self):
        element = self.point_stream.element
        groups = []
        for g, idx in self._group_data.items():
            df = element.iloc[idx].dframe()
            df[self.column] = g
            groups.append(df)
        data = pd.concat(groups).sort_values(self.column) if groups else []
        return element.clone(data, vdims=self.column).opts(plot={'color_index': self.column},
                                                           style={'cmap': 'Category20'})

    def view(self):
        table = DynamicMap(self.group_table, streams=[self.table_stream])
        annotated = DynamicMap(self.annotated_points, streams=[self.table_stream])
        return (self.tiles * self.polys * self.points * annotated + table).cols(1)


class PolyAnnotator(GeoAnnotator):
    """
    Allows drawing and annotating Polygons using a bokeh DataTable.
    """

    poly_columns = param.List(default=['Group'], doc="""
        Columns to annotate the Polygons with.""", precedence=-1)

    vertex_columns = param.List(default=[], doc="""
        Columns to annotate the Polygons with.""", precedence=-1)

    table_height = param.Integer(default=150, doc="Height of the table",
                                 precedence=-1)

    def __init__(self, poly_data={}, **params):
        super(PolyAnnotator, self).__init__(**params)
        style = dict(editable=True)
        plot = dict(width=self.width, height=self.table_height)

        # Add annotation columns to poly data
        for col in self.poly_columns:
            if col not in self.polys:
                self.polys = self.polys.add_dimension(col, 0, '', True)
        self.poly_stream.source = self.polys
        self.vertex_stream.source = self.polys
        if len(self.polys):
            poly_data = gv.project(self.polys).split()
            self.poly_stream.event(data={kd.name: [p.dimension_values(kd) for p in poly_data]
                                         for kd in self.polys.kdims})

        poly_data = {c: self.polys.dimension_values(c, expanded=False) for c in self.poly_columns}
        if len(set(len(v) for v in poly_data.values())) != 1:
            raise ValueError('poly_columns must refer to value dimensions '
                             'which vary per path while at least one of '
                             '%s varies by vertex.' % self.poly_columns)
        self.poly_table = Table(poly_data, self.poly_columns, []).opts(plot=plot, style=style)
        self.poly_link = DataLink(source=self.polys, target=self.poly_table)
        self.vertex_table = Table([], self.polys.kdims, self.vertex_columns).opts(plot=plot, style=style)
        self.vertex_link = VertexTableLink(self.polys, self.vertex_table)

    def view(self):
        return (self.tiles * self.polys * self.points + self.poly_table + self.vertex_table).cols(1)


class PointAnnotator(GeoAnnotator):
    """
    Allows drawing and annotating Points using a bokeh DataTable.
    """

    point_columns = param.List(default=['Size'], doc="""
        Columns to annotate the Points with.""", precedence=-1)

    table_height = param.Integer(default=150, doc="Height of the table",
                                 precedence=-1)

    def __init__(self, **params):
        super(PointAnnotator, self).__init__(**params)
        style = dict(editable=True)
        plot = dict(width=self.width, height=self.table_height)
        for col in self.point_columns:
            if col not in self.points:
                self.points = self.points.add_dimension(col, 0, None, True)
        self.point_stream = PointDraw(source=self.points, data={})
        projected = gv.project(self.points, projection=ccrs.PlateCarree())
        self.point_table = Table(projected).opts(plot=plot, style=style)
        self.point_link = PointTableLink(source=self.points, target=self.point_table)

    def view(self):
        return (self.tiles * self.polys * self.points + self.point_table).cols(1)


class PolyAndPointAnnotator(PolyAnnotator, PointAnnotator):
    """
    Allows drawing and annotating Points and Polygons using a bokeh
    DataTable.
    """

    def view(self):
        return(self.tiles * self.polys * self.points +
               self.poly_table + self.point_table + self.vertex_table).cols(1)


options = Store.options('bokeh')

options.Points = Options('plot', padding=0.1)
options.Path = Options('plot', padding=0.1)
options.Polygons = Options('plot', padding=0.1)
