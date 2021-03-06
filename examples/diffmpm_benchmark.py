import taichi as ti
import numpy as np
import cv2
import matplotlib.pyplot as plt
import time

real = ti.f32
ti.set_default_fp(real)
ti.cfg.enable_profiler = False
# ti.cfg.use_llvm = True

dim = 2
n_particles = 6400
N = 80
n_grid = 120
dx = 1 / n_grid
inv_dx = 1 / dx
dt = 3e-4
p_mass = 1
p_vol = 1
E = 100
# TODO: update
mu = E
la = E
max_steps = 1024
steps = 1024
gravity = 9.8
target = [0.3, 0.6]

scalar = lambda: ti.var(dt=real)
vec = lambda: ti.Vector(dim, dt=real)
mat = lambda: ti.Matrix(dim, dim, dt=real)

x, v, x_avg = vec(), vec(), vec()
grid_v_in, grid_m_in = vec(), scalar()
grid_v_out = vec()
C, F = mat(), mat()

init_v = vec()
loss = scalar()

ti.cfg.arch = ti.cuda


@ti.layout
def place():

  def p(x):
    for i in x.entries:
      ti.root.dense(ti.l, max_steps).dense(ti.k, n_particles).place(i)
      ti.root.dense(ti.l, max_steps).dense(ti.k, n_particles).place(i.grad)

  # ti.root.dense(ti.l, max_steps).dense(ti.k, n_particles).place(x, v, C, F)
  p(x)
  p(v)
  p(C)
  p(F)

  def pg(x):
    # ti.root.dense(ti.ij, n_grid // 8).dense(ti.ij, 8).place(x)
    ti.root.dense(ti.ij, n_grid).place(x)

  def pgv(x):
    for i in x.entries:
      ti.root.dense(ti.ij, n_grid).place(i)

  pgv(grid_v_in)
  pg(grid_m_in)
  pg(grid_v_out)
  # ti.root.dense(ti.ij, n_grid).place(grid_v_in, grid_m_in, grid_v_out)
  # ti.root.dense(ti.ij, n_grid).place(grid_v_in, grid_m_in, grid_v_out)
  ti.root.place(init_v, loss, x_avg)

  ti.root.lazy_grad()


@ti.kernel
def set_v():
  for i in range(n_particles):
    v[0, i] = init_v


@ti.kernel
def clear_grid():
  for i, j in grid_m_in:
    grid_v_in[i, j] = [0, 0]
    grid_m_in[i, j] = 0
    grid_v_in.grad[i, j] = [0, 0]
    grid_m_in.grad[i, j] = 0
    grid_v_out.grad[i, j] = [0, 0]


@ti.kernel
def p2g(f: ti.i32):
  for p in range(0, n_particles):
    base = ti.cast(x[f, p] * inv_dx - 0.5, ti.i32)
    fx = x[f, p] * inv_dx - ti.cast(base, ti.i32)
    w = [0.5 * ti.sqr(1.5 - fx), 0.75 - ti.sqr(fx - 1), 0.5 * ti.sqr(fx - 0.5)]
    new_F = (ti.Matrix.diag(dim=2, val=1) + dt * C[f, p]) @ F[f, p]
    F[f + 1, p] = new_F
    J = ti.determinant(new_F)
    r, s = ti.polar_decompose(new_F)
    cauchy = 2 * mu * (new_F - r) @ ti.transposed(new_F) + \
             ti.Matrix.diag(2, la * (J - 1) * J)
    stress = -(dt * p_vol * 4 * inv_dx * inv_dx) * cauchy
    affine = stress + p_mass * C[f, p]
    for i in ti.static(range(3)):
      for j in ti.static(range(3)):
        offset = ti.Vector([i, j])
        dpos = (ti.cast(ti.Vector([i, j]), real) - fx) * dx
        weight = w[i](0) * w[j](1)
        grid_v_in[base + offset].atomic_add(
            weight * (p_mass * v[f, p] + affine @ dpos))
        grid_m_in[base + offset].atomic_add(weight * p_mass)


bound = 3


@ti.kernel
def grid_op():
  for p in range(n_grid * n_grid):
    i = p // n_grid
    j = p - n_grid * i
    inv_m = 1 / (grid_m_in[i, j] + 1e-10)
    v_out = inv_m * grid_v_in[i, j]
    v_out[1] -= dt * gravity
    if i < bound and v_out[0] < 0:
      v_out[0] = 0
    if i > n_grid - bound and v_out[0] > 0:
      v_out[0] = 0
    if j < bound and v_out[1] < 0:
      v_out[1] = 0
    if j > n_grid - bound and v_out[1] > 0:
      v_out[1] = 0
    grid_v_out[i, j] = v_out


@ti.kernel
def g2p(f: ti.i32):
  for p in range(n_particles):
    base = ti.cast(x[f, p] * inv_dx - 0.5, ti.i32)
    fx = x[f, p] * inv_dx - ti.cast(base, real)
    w = [
        0.5 * ti.sqr(1.5 - fx), 0.75 - ti.sqr(fx - 1.0), 0.5 * ti.sqr(fx - 0.5)
    ]
    new_v = ti.Vector([0.0, 0.0])
    new_C = ti.Matrix([[0.0, 0.0], [0.0, 0.0]])

    for i in ti.static(range(3)):
      for j in ti.static(range(3)):
        dpos = ti.cast(ti.Vector([i, j]), real) - fx
        g_v = grid_v_out[base(0) + i, base(1) + j]
        weight = w[i](0) * w[j](1)
        new_v += weight * g_v
        new_C += 4 * weight * ti.outer_product(g_v, dpos) * inv_dx

    v[f + 1, p] = new_v
    x[f + 1, p] = x[f, p] + dt * v[f + 1, p]
    C[f + 1, p] = new_C


@ti.kernel
def compute_x_avg():
  for i in range(n_particles):
    x_avg[None].atomic_add((1 / n_particles) * x[steps - 1, i])


@ti.kernel
def compute_loss():
  dist = ti.sqr(x_avg - ti.Vector(target))
  loss[None] = 0.5 * (dist(0) + dist(1))


@ti.complex_kernel
def substep(s):
  clear_grid()
  p2g(s)
  grid_op()
  g2p(s)


@ti.complex_kernel_grad(substep)
def substep_grad(s):
  clear_grid()
  p2g(s)
  grid_op()

  g2p.grad(s)
  grid_op.grad()
  p2g.grad(s)


def benchmark():
  print(
      'Also check "nvprof --print-gpu-trace python3 diffmpm_benchmark.py" for more accurate results'
  )
  iters = 100000
  for i in range(1):
    p2g(0)
    grid_op()
    g2p(0)
  t = time.time()
  ti.runtime.sync()
  for i in range(iters):
    # clear_grid()
    p2g(0)
    grid_op()
    g2p(0)
  ti.runtime.sync()
  print('forward ', (time.time() - t) / iters * 1000 * 3, 'ms')
  ti.profiler_print()

  for i in range(1):
    p2g.grad(0)
    grid_op.grad()
    g2p.grad(0)
  t = time.time()
  ti.runtime.sync()
  for i in range(iters):
    # clear_grid()
    g2p.grad(0)
    grid_op.grad()
    p2g.grad(0)
  ti.runtime.sync()
  print('backward ', (time.time() - t) / iters * 1000 * 3, 'ms')
  ti.profiler_print()


def main():
  # initialization
  init_v[None] = [0, 0]

  for i in range(n_particles):
    F[0, i] = [[1, 0], [0, 1]]

  for i in range(N):
    for j in range(N):
      x[0, i * N + j] = [dx * (i * 0.5 + 10), dx * (j * 0.5 + 25)]

  set_v()
  benchmark()

  losses = []
  img_count = 0
  for i in range(30):
    with ti.Tape(loss=loss):
      set_v()
      for s in range(steps - 1):
        substep(s)

      loss[None] = 0
      x_avg[None] = [0, 0]
      compute_x_avg()
      compute_loss()
    l = loss[None]
    losses.append(l)
    grad = init_v.grad[None]
    print('loss=', l, '   grad=', (grad[0], grad[1]))
    learning_rate = 10
    init_v(0)[None] -= learning_rate * grad[0]
    init_v(1)[None] -= learning_rate * grad[1]

    # visualize
    for s in range(63, steps, 64):
      scale = 4
      img = np.zeros(shape=(scale * n_grid, scale * n_grid)) + 0.3
      total = [0, 0]
      for i in range(n_particles):
        p_x = int(scale * x(0)[s, i] / dx)
        p_y = int(scale * x(1)[s, i] / dx)
        total[0] += p_x
        total[1] += p_y
        img[p_x, p_y] = 1
      cv2.circle(
          img, (total[1] // n_particles, total[0] // n_particles),
          radius=5,
          color=0,
          thickness=5)
      cv2.circle(
          img,
          (int(target[1] * scale * n_grid), int(target[0] * scale * n_grid)),
          radius=5,
          color=1,
          thickness=5)
      img = img.swapaxes(0, 1)[::-1]
      cv2.imshow('MPM', img)
      img_count += 1
      # cv2.imwrite('MPM{:04d}.png'.format(img_count), img * 255)
      cv2.waitKey(1)
    ti.profiler_print()

  ti.profiler_print()
  plt.title("Optimization of Initial Velocity")
  plt.ylabel("Loss")
  plt.xlabel("Gradient Descent Iterations")
  plt.plot(losses)
  plt.show()


if __name__ == '__main__':
  main()
