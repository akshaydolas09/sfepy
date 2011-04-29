import numpy as nm

from sfepy.base.base import assert_
from sfepy.linalg import dot_sequences
from sfepy.mechanics.tensors import dim2sym, transform_data
import sfepy.mechanics.membranes as membranes
from sfepy.fem.poly_spaces import PolySpace
from sfepy.terms.terms import Term

def eval_membrane_mooney_rivlin(a1, a2, mtx_c, c33, mode):
    """
    Evaluate stress or tangent stiffness of the Mooney-Rivlin membrane.

    [1] Baoguo Wu, Xingwen Du and Huifeng Tan: A three-dimensional FE
    nonlinear analysis of membranes, Computers & Structures 59 (1996),
    no. 4, 601--605.
    """
    a12 = 2.0 * a1[..., 0, 0]
    a22 = 2.0 * a2[..., 0, 0]

    sh = mtx_c.shape
    sym = dim2sym(sh[2])

    c11 = mtx_c[..., 0, 0]
    c12 = mtx_c[..., 0, 1]
    c22 = mtx_c[..., 1, 1]
    pressure = c33 * (a12 + a22 * (c11 + c22))

    if mode == 0:
        out = nm.empty((sh[0], sh[1], sym, 1))

        # S_11, S_22, S_12.
        out[..., 0, 0] = -pressure * c22 * c33 + a12 + a22 * (c22 + c33)
        out[..., 1, 0] = -pressure * c11 * c33 + a12 + a22 * (c11 + c33)
        out[..., 2, 0] = +pressure * c12 * c33 - a22 * c12

    else:
        out = nm.empty((sh[0], sh[1], sym, sym))

        dp11 = a22 * c33 - pressure * c22 * c33
        dp22 = a22 * c33 - pressure * c11 * c33
        dp12 = 2.0 * pressure * c12 * c33

        # D_11, D_22, D_33
        out[..., 0, 0] = - 2.0 * ((a22 - pressure * c22) * c22 * c33**2
                                  + c33 * c22 * dp11)
        out[..., 1, 1] = - 2.0 * ((a22 - pressure * c11) * c11 * c33**2
                                  + c33 * c11 * dp22)
        out[..., 2, 2] = - a22 + pressure * (c33 + 2.0 * c12**2 * c33**2) \
                         + c12 * c33 * dp12

        # D_21, D_31, D_32
        out[..., 1, 0] = 2.0 * ((a22 - pressure * c33
                                 - (a22 - pressure * c11) * c22 * c33**2)
                                - c33 * c11 * dp11)
        out[..., 2, 0] = 2.0 * (-pressure * c12 * c22 * c33**2
                                + c12 * c33 * dp11)
        out[..., 2, 1] = 2.0 * (-pressure * c12 * c11 * c33**2
                                + c12 * c33 * dp22)

        out[..., 0, 1] = out[..., 1, 0]
        out[..., 0, 2] = out[..., 2, 0]
        out[..., 1, 2] = out[..., 2, 1]

        # D_12, D_13, D_23
        ## out[..., 0, 1] = 2.0 * ((a22 - pressure * c33
        ##                          - (a22 - pressure * c22) * c11 * c33**2)
        ##                         - c33 * c22 * dp22)
        ## out[..., 0, 2] = 2.0 * (a22 - pressure * c22) * c12 * c33**2 \
        ##                  - c33 * c22 * dp12
        ## out[..., 1, 2] = 2.0 * (a22 - pressure * c11) * c12 * c33**2 \
        ##                  - c33 * c11 * dp12

    return out

class TLMembraneTerm(Term):
    r"""
    Mooney-Rivlin membrane with plain stress assumption.

    :Arguments:
        virtual  : :math:`\ul{v}`,
        state    : :math:`\ul{u}`
    """
    name = 'dw_tl_membrane'
    arg_types = ('material_a1', 'material_a2', 'material_h0',
                 'virtual', 'state')
    integration = 'surface'

    def __init__(self, *args, **kwargs):
        Term.__init__(self, *args, **kwargs)

        igs = self.region.igs
        self.mtx_t = {}.fromkeys(igs, None)
        self.membrane_geo = {}.fromkeys(igs, None)
        self.bfg = {}.fromkeys(igs, None)

    def describe_membrane_geometry(self, ig, field, sg, sd):
        # Coordinates of element vertices.
        coors = field.coors[sd.econn[:, :sg.nFP]]

        # Coordinate transformation matrix (transposed!).
        self.mtx_t[ig] = membranes.create_transformation_matrix(coors)

        # Transform coordinates to the local coordinate system.
        coors_loc = dot_sequences((coors - coors[:, 0:1, :]), self.mtx_t[ig])

        # Mapping from transformed element to reference element.
        gel = field.gel.surface_facet
        vm = membranes.create_mapping(coors_loc, gel, 1)

        qp = self.integral.get_qp(gel.name)
        ps = PolySpace.any_from_args(None, gel, field.approx_order)
        self.membrane_geo[ig] = vm.get_mapping(*qp, poly_space=ps)

        # Transformed base function gradient w.r.t. material coordinates
        # in quadrature points.
        self.bfg[ig] = self.membrane_geo[ig].variable(0)

    def get_shape(self, ap, diff_var, chunk_size):
        n_el, n_qp, dim, n_ep = ap.get_s_data_shape(self.integral,
                                                    self.region.name)
        assert_(dim == 3)

        if diff_var is None:
            return (chunk_size, 1, dim * n_ep, 1), 0

        elif diff_var == self.get_arg_name( 'state' ):
            return (chunk_size, 1, dim * n_ep, dim * n_ep), 1

        else:
            raise StopIteration

    def __call__(self, diff_var=None, chunk_size=None, **kwargs):
        """
        Membrane term evaluation function.
        """
        term_mode, = self.get_kwargs(['term_mode'], **kwargs)
        a1, a2, h0, vv, vu = self.get_args(**kwargs)
        ap, sg = self.get_approximation(vv)
        sd = ap.surface_data[self.region.name]

        ig = self.char_fun.ig
        if self.mtx_t[ig] is None:
            self.describe_membrane_geometry(ig, vu.field, sg, sd)

        mtx_t = self.mtx_t[ig]
        bfg = self.bfg[ig]
        geo = self.membrane_geo[ig]

        # Displacements of element nodes.
        vec_u = vu.get_state_in_region(self.region, igs=[self.char_fun.ig])
        el_u = vec_u[sd.leconn]

        # Transform displacements to the local coordinate system.
        # u_new = T^T u
        el_u_loc = dot_sequences(el_u, mtx_t, 'AB')
        ## print el_u_loc

        n_ep = bfg.shape[3]

        mtx_c, c33, mtx_b = membranes.describe_deformation(el_u_loc, bfg)

        shape, mode = self.get_shape(ap, diff_var, chunk_size)

        if term_mode is None:

            crt = eval_membrane_mooney_rivlin(a1, a2, mtx_c, c33, mode)

            if mode == 0:
                bts = dot_sequences(mtx_b, crt, 'ATB')

                for out, chunk in self.char_fun(chunk_size, shape):
                    lchunk = self.char_fun.get_local_chunk()
                    status = geo.integrate_chunk(out, bts * h0, lchunk)

                    # Transform to global coordinate system, one node at
                    # a time.
                    for iep in range(n_ep):
                        ir = slice(iep, None, n_ep)
                        fn = out[:, 0, ir, 0]
                        fn[:] = dot_sequences(mtx_t[lchunk], fn, 'AB')

                    yield out, lchunk, 0

            else:
                btd = dot_sequences(mtx_b, crt, 'ATB')
                btdb = dot_sequences(btd, mtx_b)

                stress = eval_membrane_mooney_rivlin(a1, a2, mtx_c, c33, 0)

                kts =  membranes.get_tangent_stress_matrix(stress, bfg)

                mtx_k = kts + btdb

                for out, chunk in self.char_fun(chunk_size, shape):
                    lchunk = self.char_fun.get_local_chunk()
                    status = geo.integrate_chunk(out, mtx_k * h0, lchunk)

                    # Transform to global coordinate system, one node at
                    # a time.
                    dot = dot_sequences
                    tc = mtx_t[lchunk]
                    for iepr in range(n_ep):
                        ir = slice(iepr, None, n_ep)
                        for iepc in range(n_ep):
                            ic = slice(iepc, None, n_ep)
                            fn = out[:, 0, ir, ic]
                            fn[:] = dot(dot(tc, fn, 'AB'), tc, 'ABT')

                    yield out, lchunk, 0

        elif term_mode in ['strain', 'stress']:
            if term_mode == 'strain':
                out_qp = membranes.get_green_strain_sym3d(mtx_c, c33)

            elif term_mode == 'stress':
                n_el, n_qp, dm, _ = mtx_c.shape
                dim = dm + 1
                sym = dim2sym(dim)
                out_qp = nm.zeros((n_el, n_qp, sym, 1), dtype=mtx_c.dtype)

                stress = eval_membrane_mooney_rivlin(a1, a2, mtx_c, c33, 0)
                out_qp[..., 0:2, 0] = stress[..., 0:2, 0]
                out_qp[..., 3, 0] = stress[..., 2, 0]

            shape = (chunk_size, 1) + out_qp.shape[2:]
            for out, chunk in self.char_fun(chunk_size, shape):
                lchunk = self.char_fun.get_local_chunk()
                status = geo.integrate_chunk(out, out_qp[lchunk], lchunk)
                out = transform_data(out, mtx=mtx_t)
                out1 = out / geo.variable(2)[lchunk]

            yield out1, chunk, status